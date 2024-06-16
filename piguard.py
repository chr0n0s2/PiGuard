#!/usr/bin/python3

# ------------------------ COMENTARIO DE ENCABEZADO ------------------------
# Script: Pi Guard
# Descripción: Este script monitorea la conexión de una UPS a una Raspberry Pi
#              y apaga el sistema en caso de una pérdida de energía prolongada.
# Requerimientos del sistema: Python 3, RPi.GPIO, smbus
# Autor: Roy Jaque

import sys
import os
import signal
import atexit
import time
import logging
import logging.handlers
import argparse
import RPi.GPIO as GPIO
import datetime
import subprocess   # Se usa en isr para envio de comandos de forma segura
import smbus        # Libreria para i2c
import threading    # Para aplicar Mutex para PULSE_PIN


class PiGuard:
    # Define umbrales constantes para los valores ingresados por usuario
    MAX_SHUTDOWN_DELAY  = 235           # Tiempo máximo de espera antes de apagar (hrs)
    MIN_SHUTDOWN_DELAY  = 1             # Tiempo mínimo de espera antes de apagar (en minutos)
    MAX_WATCHDOG_RPI    = 10            # Tiempo máximo de espera antes de reiniciar por RPI colgada (en minutos)
    MIN_WATCHDOG_RPI    = 3             # Tiempo mínimo de espera antes de reiniciar por RPI colgada (en minutos)
    MAX_LOOP_RUN_UPS    = 20            # Tiempo máximo de espera dentro del bucle principal (en segundos)
    MIN_LOOP_RUN_UPS    = 1             # Tiempo mínimo de espera dentro del bucle principal (en segundos)
    MAX_POST_SHUTDOWN   = 254           # Tiempo máximo después de la señal de apagado para que la RPi termine sus procesos antes de desconectar la energía. (en segundos)
    MIN_POST_SHUTDOWN   = 10            # Tiempo mínimo después de la señal de apagado para que la RPi termine sus procesos antes de desconectar la energía. (en segundos)
    MAX_FREC_HISTORY    = 3600          # Tiempo máximo de muestreo y escritura del historial (en segundos)
    MIN_FREC_HISTORY    = 20            # Tiempo mínimo de muestreo y escritura del historial (en segundos)
    
    # Comandos I2C de la UPS
    READ_VOLTAGE_CMD        = 0x01
    READ_MODE_CMD           = 0x02
    SET_SHUTDOWN_TIME_CMD   = 0x03
    WATCHDOG_RPI_CMD        = 0x04
    POST_SHUTDOWN_CMD       = 0x05
      
    # Dirección I2C de la UPS
    DIR_I2C                 = 0x20

    BOUNCE_TIME = 30                    # Define el tiempo ignorado por rebote mecánico de la GPIO (en milisegundos)
    CLOCK_PIN   = 27                    # GPIO pin for UPS clock signal
    PULSE_PIN   = 22                    # GPIO pin for UPS pulse signal
    LOGLEVEL    = "INFO"                # Log level for the script (info, debug, etc.)


    
     
    def __init__(self):

        # -------------------- CONFIGURACIÓN DESDE ARCHIVO --------------------

        # Ruta donde se almacena la ruta al archivo de configuración 
        self.path_config_file = "/usr/local/bin/path_config.txt"
        
        # Se declara el logger
        self.log = logging.getLogger(__name__)   
        
        # Variable para inicializar el bus I2C
        self.bus = None  
        
        # Crear un mutex para proteger el acceso a PULSE_PIN (en caso de ser necesario)                        
        self.pulse_pin_mutex = threading.Lock() 
        
        # Define frecuencia del log por defecto (en segundos)
        self.FREC_HISTORY = 60 
         
        # Valor inicial para history_write para escribir de inmediato al iniciar
        self.history_write = (float)(time.time() - self.FREC_HISTORY)  
          
        # Valor por defecto de onda cuadrada en alto
        self.sqwave = True
        
        # Carga la configuración del archivo
        self.load_config()
        
    def load_config(self):
            
        # Lee path_config_file y busca la ruta deseada al archivo de configuración
        with open(self.path_config_file, "r") as f:
            for line in f:
                if line.startswith("DEFAULT_CONFIG_FILE"):
                    self.config_file = line.split("=")[1].strip()
                    break

        # Extraer el directorio base (home_dir)
        self.home_dir = os.path.dirname(self.config_file)

        # Verifica si existe piguard_config.txt, sino lo crea con valores predeterminados
        if not os.path.exists(self.config_file):
            with open(self.config_file, "w") as f:
                f.write("""# Configuration for the Pi Guard script
# Shutdown delay time before power-off (from 1-180 in minutes, from 181 to 235 in hours, e.g., 181=4hrs, 182=5hrs ... 235=58hrs)
SHUTDOWN_DELAY = 60

# WATCHDOG. The Raspberry Pi has frozen (in minutes) before power-off warning (3-10) It cannot be set to less than 3 minutes because, if the UPS is connected, it will cause rapid reboots, making it impossible to fix any installation issues remotely.
WATCHDOG_RPI = 5

# Waiting time (in seconds) within the main loop (1-20) Be careful not to overload the system.
LOOP_RUN_UPS = 5

# Time after shutdown signal (in seconds) for RPi to finish its processes before disconnecting power (10-254)
POST_SHUTDOWN = 30

# Data read frequency for writing the log, in seconds (20-3600)
FREC_HISTORY = 60

# Enable/disable sending I2C commands (True/False). Only disable if you want to take control of the UPS using I2C commands from another program.
I2C_SEND_ENABLED = True

""")
            # Otorgar permisos de lectura a otros usuarios
            os.chmod(self.config_file, 0o666)  

        # Lee el archivo piguard_config.txt y rescata sus datos.
        self.config_vars = {}

        with open(self.config_file, "r") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                key, value = line.strip().split("=")
                self.config_vars[key.strip()] = value.strip()
                
        # Compara los valores rescatados con los limites para seguridad de su ejecucion
        self.SHUTDOWN_DELAY = max(self.MIN_SHUTDOWN_DELAY, min(int(self.config_vars["SHUTDOWN_DELAY"]), self.MAX_SHUTDOWN_DELAY))
        self.WATCHDOG_RPI = max(self.MIN_WATCHDOG_RPI, min(int(self.config_vars["WATCHDOG_RPI"]), self.MAX_WATCHDOG_RPI))
        self.LOOP_RUN_UPS = max(self.MIN_LOOP_RUN_UPS, min(int(self.config_vars["LOOP_RUN_UPS"]), self.MAX_LOOP_RUN_UPS))
        self.POST_SHUTDOWN = max(self.MIN_POST_SHUTDOWN, min(int(self.config_vars["POST_SHUTDOWN"]), self.MAX_POST_SHUTDOWN))
        self.FREC_HISTORY = max(self.MIN_FREC_HISTORY, min(float(self.config_vars["FREC_HISTORY"]), self.MAX_FREC_HISTORY))

        # Asegurar que la activacion del i2c este en minusculas
        self.I2C_SEND_ENABLED = self.config_vars.get("I2C_SEND_ENABLED", "True").lower() == "true"

        # Guarda la configuración actualizada por los límites en el archivo piguard_config.txt
        with open(self.config_file, "r") as f_read:
            config_lines = f_read.readlines()

        with open(self.config_file, "w") as f_write:
            for line in config_lines:
                if line.startswith("#") or not line.strip():
                    f_write.write(line)  # Escribe los comentarios sin cambios
                else:
                    key, value = line.strip().split("=")
                    new_value = self.config_vars.get(key.strip(), value.strip())
                    f_write.write(f"{key.strip()} = {new_value}\n")

        # -------------------- FIN CONFIGURACIÓN DESDE ARCHIVO --------------------
        # Ejecuta el comando 'uptime -s' para obtener la hora de inicio del sistema
        start_time_str = subprocess.check_output(['uptime', '-s']).decode('utf-8').strip()

        # Convierte la cadena de la hora de inicio a un objeto datetime
        start_time = datetime.datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')

        # Formatea el tiempo de inicio al formato que necesitas
        formatted_start_time = start_time.strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]

        # Escribir la hora de encendido en el archivo UPS_History_POWER_OFF
        with open(os.path.join(self.home_dir, "UPS_History_POWER.dbg"), "a+") as file:
            file.write(formatted_start_time + "\tThe Raspberry Pi has started.\n")
    
       
    def isr(self, channel): # Se llama a la interrupción a los ritmos del CLOCK_PIN
        """
        Rutina de servicio de interrupción GPIO (ISR) se ejecuta cuando se detecta una caída en el CLOCK_PIN.
        """
        # El PIN que llamo a la interrupcion es channel, se verifica que sea CLOCK_PIN y no otro HAT que pudiera conectar el usuario
        if channel != self.CLOCK_PIN:
            return
        
        # Adquirir el mutex para proteger la sección crítica
        with self.pulse_pin_mutex:      
      
            # Se lee PULSE_PIN como salida, y se guarda su valor invertido antes de revisar PULSEPIN como entrada
            self.sqwave = not GPIO.input(self.PULSE_PIN)
                
            # PulsePin=0 antes de cambiarlo a entrada para buscar la señal de apagado.
            GPIO.output(self.PULSE_PIN, False)
            
            # Cambiar la configuración de PULSE_PIN a entrada
            GPIO.setup(self.PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                
            # SE LEE PULSEPIN COMO ENTRADA (UPS -> RPI) SE REALIZA APAGADO (si Pulsepin=0)
            if not GPIO.input(self.PULSE_PIN):
                self.log.warning("Se solicita el apagado de la Raspberry Pi desde la UPS")
                try:
                    with open(os.path.join(self.home_dir, "UPS_History_POWER.dbg"), "a+") as fb:
                        now = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]
                        fb.write(f"{now}\n\tThe Volta UPS commands the shutdown of the Raspberry Pi.\n\n")

                except Exception as e:
                    self.log.error("Error al escribir en el archivo de registro: %s", e)

                # Apagar si el bucle termina 
                time.sleep(2) 
                #os.system('/sbin/shutdown -h now')
                subprocess.call(['/sbin/shutdown', '-h', 'now'])
                    
            # Revertir PULSE_PIN a salida, y se invierte su valor con sqwave por cada isr, por cada vez que esta en 1 se evita apagado por watchdog
            GPIO.setup(self.PULSE_PIN, GPIO.OUT, initial=self.sqwave)
            
    def read_voltage(self):
        """Lee el voltaje de la batería desde la UPS."""        
        try:
            # Enviar comando 0x01 para obtener voltaje
            self.bus.write_byte(self.DIR_I2C, self.READ_VOLTAGE_CMD)
            time.sleep(0.2)  
            vol = self.bus.read_byte(self.DIR_I2C)
            return vol
        except IOError as e:
            self.log.error("Error de I/O al leer el voltaje: %s", e)
            return

    def read_ups_mode(self):
        """Lee el modo de la UPS (batería o alimentación externa)."""
        try:
            # Enviar comando 0x02 para obtener modo
            self.bus.write_byte(self.DIR_I2C, self.READ_MODE_CMD)
            time.sleep(0.2)  
            modo = self.bus.read_byte(self.DIR_I2C)
            return modo
        except IOError as e:
            self.log.error("Error de I/O al leer el modo: %s", e)
            return

    # Configurar tiempo de apagado al inicio 
    def set_shutdown_time(self):
        """Configura el tiempo de apagado en la UPS (en minutos)."""
        try:
            # Validar que SHUTDOWN_DELAY sea un entero válido
            if not isinstance(self.SHUTDOWN_DELAY, int):
                raise ValueError("SHUTDOWN_DELAY debe ser un entero")
            
            # Enviar comando 0x03 con el tiempo en minutos
            self.bus.write_byte(self.DIR_I2C, self.SET_SHUTDOWN_TIME_CMD)
            
            # Pequeña pausa para que la UPS procese el comando
            time.sleep(0.2)  
            
            # Envia el tiempo SHUTDOWN_DELAY
            self.bus.write_byte(self.DIR_I2C, self.SHUTDOWN_DELAY)
            
            # Buscar forma de verificar si el comando se ejecutó correctamente (esto depende de la implementación de tu UPS)
        except IOError as e:
            self.log.error("Error de I/O al escribir el shutdown time: %s", e)
            return
            
    def set_watchdog_time(self):
        """Configura el tiempo de apagado por watchdog en la UPS (en minutos)."""
        try:
            # Enviar comando 0x04 con el tiempo en minutos
            self.bus.write_byte(self.DIR_I2C, self.WATCHDOG_RPI_CMD)
            
            # Pequeña pausa para que la UPS procese el comando
            time.sleep(0.2) 
             
            # Envia el tiempo WATCHDOG_RPI
            self.bus.write_byte(self.DIR_I2C, self.WATCHDOG_RPI)
            
            # Buscar forma de verificar si el comando se ejecutó correctamente (esto depende de la implementación de tu UPS)     
        except IOError as e:
            self.log.error("Error de I/O al escribir el watchdog time: %s", e)
            return
        
    def set_post_shutdown(self):
        """Configura el tiempo posterior a la orden de apagado hasta que la UPS corta la energía (en segundos)."""
        try:
            # Enviar comando 0x05 con el tiempo en segundos
            self.bus.write_byte(self.DIR_I2C, self.POST_SHUTDOWN_CMD)
            
            # Pequeña pausa para que la UPS procese el comando
            time.sleep(0.2) 
             
            # Envia el tiempo POST_SHUTDOWN
            self.bus.write_byte(self.DIR_I2C, self.POST_SHUTDOWN)
            
            # Buscar forma de verificar si el comando se ejecutó correctamente (esto depende de la implementación de tu UPS)     
        except IOError as e:
            self.log.error("Error de I/O al escribir el post shutdown time: %s", e)
            return

    def main(self):
        """
        Función principal del script UPS Volta
        """
        # Crea un analizador de argumentos utilizando la librería argparse
        parser = argparse.ArgumentParser()

        # Define el argumento opcional '--log-level' para especificar el nivel de registro (info o debug)
        parser.add_argument('-l','--log-level', choices=['info', 'debug'], default='info', help="Log level, ('info' o 'debug')") 

        # Agrega el argumento opcional '--debug' para ejecutar en primer plano (sin daemonizar)
        parser.add_argument('-d','--debug', action='store_true', help='Mantener en primer plano, no ejecutar como demonio') 

        # Analiza los argumentos pasados en la línea de comandos y los almacena en el objeto 'args'
        args = parser.parse_args()  

        # Configura el registro (logging) para el demonio segun lo solicitado DEBUG o INFO
        numeric_level = getattr(logging, args.log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Nivel de registro inválido: {}'.format(args.log_level))
            
        # Este es el nivel de los registros
        self.log.setLevel(numeric_level)
        
        # Agregar un nuevo manejador que envía registros a la salida estándar (stdout)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self.log.addHandler(handler)

        # Inicializar el bus I2C
        try:
            self.bus = smbus.SMBus(1)  # Crea una instancia de smbus.SMBus
        except Exception as e:
            self.log.error("Error al inicializar el bus I2C: %s", e)
            return

        # Inicializar los pines GPIO dentro de un bloque try-except
        try:
            #GPIO.cleanup()
            GPIO.setmode(GPIO.BCM)
            #GPIO.setwarnings(False)

            GPIO.setup(self.CLOCK_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP) # Se inicializa como entrada para recibir el clock desde la UPS
              
            GPIO.setup(self.PULSE_PIN, GPIO.OUT, initial=True)  # Inicializa en HIGH, osea en la interrupción al PIC por PULSEPIN para evitar el reinicio por watchdog.
            
            GPIO.add_event_detect(self.CLOCK_PIN, GPIO.FALLING, callback=self.isr, bouncetime=self.BOUNCE_TIME) # Se deja activa la detección de interrupciónes por cada Clock antes de entrer en el bucle infinito
            
        except Exception as e:
            self.log.critical(f"Error al configurar los pines GPIO: {e}")
            sys.exit(1)
        
            
        # Configurar los tiempos de apagado (Se deben actualizar en el codigo de PIC al inicio)
        if self.I2C_SEND_ENABLED:
            self.set_shutdown_time()    # Escribe Shutdown Delay
            self.set_watchdog_time()    # Escribe Watchdog RPI
            self.set_post_shutdown()    # Escribe Post Shutdown
            
            with open(os.path.join(self.home_dir, "UPS_History_POWER.dbg"), "a+") as f:
                now = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]
                if self.SHUTDOWN_DELAY<181:
                    f.write(f"{now}\n\tShutdown delay: \t{self.SHUTDOWN_DELAY}\t[min]\n\tWatchdog Rpi: \t\t{self.WATCHDOG_RPI}\t[min]\n\tPost Shutdown: \t\t{self.POST_SHUTDOWN}\t[s]\n")
                else:
                    f.write(f"{now}\n\tShutdown delay: \t{self.SHUTDOWN_DELAY-177}\t[hrs]\n\tWatchdog Rpi: \t\t{self.WATCHDOG_RPI}\t[min]\n\tPost Shutdown: \t\t{self.POST_SHUTDOWN}\t[s]\n")
        time.sleep(15)
 
        # Bucle infinito para esperar interrupciones   
        try:
            last_mode = None  # Inicializa la variable para almacenar el último modo leído
        
            while True:
                if self.I2C_SEND_ENABLED:
                    voltage = self.read_voltage()     # Leer voltaje
                    mode = self.read_ups_mode()       # Leer modo UPS      
                    
                    # Verificar que voltage es un valor válido antes de convertirlo a float
                    if voltage is not None and mode is not None:
                        # Escribir historial cada minuto
                        volta=float(voltage) * 0.02
                        
                        now = time.time()
                        if now - self.history_write >= self.FREC_HISTORY:   
                            self.history_write = now   
                            # Verificar si hay un cambio en el modo y escribir solo si es necesario
                            if last_mode is None or last_mode != mode:
                                try:
                                    with open(os.path.join(self.home_dir, "UPS_History_POWER.dbg"), "a+") as fb:
                                        now = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]
                                        fb.write(f"{now}\tVoltaje: {volta:.1f}[V]\t\tModo: {mode}\n")
                                        last_mode = mode  # Actualizar el último modo leído 
                                except Exception as e:
                                    self.log.error("Error al escribir en UPS_History_POWER.dbg: %s", e)
                            with open(os.path.join(self.home_dir, "UPS_History.dbg"), "a+") as f:
                                now = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")[:-3]
                                f.write(f"{now}\tVoltaje: {volta:.1f}[V]\t\tModo: {mode}\n")    #Externa o batería
                time.sleep(self.LOOP_RUN_UPS)  # Usa la variable de configuración   
        except KeyboardInterrupt:
            self.log.info("Deteniendo el servicio Pi Guard...")
        finally:
            self.log.info("Limpiando los pines GPIO...")
            GPIO.cleanup()

if __name__ == "__main__":
    ups= PiGuard()
    ups.main()   
