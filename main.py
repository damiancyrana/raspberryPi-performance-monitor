import time
import shutil
import logging
import subprocess
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import sh1106
from gpiozero import CPUTemperature

logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')


class RaspberryMonitor:
    """
    Class responsible for monitoring various system resources like CPU temperature, CPU usage, RAM usage, and network speed on a Raspberry Pi
    """
    def __init__(self):
        self.cpu_temp_sensor = CPUTemperature()
        self.prev_cpu_stats = None
        self.prev_net_stats = {'rx': 0, 'tx': 0}
        self.mem_info = self.get_memory_info()
        self.net_interface = self.get_active_network_interface()

    def get_active_network_interface(self):
        """
        Determines the currently active network interface by inspecting the default route of the system
        """
        try:
            process = subprocess.run(['ip', 'route', 'get', '1'], capture_output=True, text=True)
            if process.returncode == 0:
                for line in process.stdout.splitlines():
                    if 'dev' in line:
                        parts = line.split()
                        return parts[parts.index('dev') + 1]
            else:
                logging.error("Failed to get default network interface")
        except Exception as e:
            logging.error(f"Exception occurred while getting active network interface: {e}")
        return 'wlan0' # Default monitor 'wlan0' if unable to determine the interface
    
    def get_network_speed(self):
        """
        Calculates the network download and upload speed based on the change in transmitted and received bytes over time
        """
        self.net_interface = self.get_active_network_interface()
        rx_bytes = int(self.get_first_line(f'/sys/class/net/{self.net_interface}/statistics/rx_bytes'))
        tx_bytes = int(self.get_first_line(f'/sys/class/net/{self.net_interface}/statistics/tx_bytes'))

        rx_speed_kb = (rx_bytes - self.prev_net_stats['rx']) / 1024
        tx_speed_kb = (tx_bytes - self.prev_net_stats['tx']) / 1024

        self.prev_net_stats.update({'rx': rx_bytes, 'tx': tx_bytes})
        return rx_speed_kb, tx_speed_kb

    def get_first_line(self, path):
        """
        Reads the first line from a file located at the given path
        """
        try:
            with open(path, 'r') as file:
                return file.readline().strip()
        except IOError:
            logging.error(f"Error reading file {path}")
            return ''

    def get_cpu_usage(self):
        """
        Calculates the CPU usage percentage by reading system stats
        """
        line = self.get_first_line('/proc/stat')
        cpu_stats = [int(value) for value in line.split()[1:]]

        if not self.prev_cpu_stats:
            self.prev_cpu_stats = cpu_stats
            return 0.0

        deltas = [current - prev for current, prev in zip(cpu_stats, self.prev_cpu_stats)]
        total_time = sum(deltas)
        idle_time = deltas[3]

        self.prev_cpu_stats = cpu_stats
        return (1 - idle_time / total_time) * 100 if total_time else 0

    def get_cpu_temp(self):
        """
        Retrieves the current CPU temperature from the sensor
        """
        return self.cpu_temp_sensor.temperature

    def get_memory_info(self):
        """
        Reads and parses memory information from the system's '/proc/meminfo' file
        """
        memory_info = {}
        try:
            with open('/proc/meminfo', 'r') as file:
                for line in file:
                    key, value, *_ = line.split()
                    memory_info[key.rstrip(':')] = int(value)
        except IOError:
            logging.error("Error reading memory information")
        return memory_info

    def get_ram_usage(self):
        """
        Calculates the RAM usage percentage using the memory information
        """
        total_memory = self.mem_info.get('MemTotal', 1)
        available_memory = self.mem_info.get('MemAvailable', 0)
        used_memory = total_memory - available_memory
        return (used_memory / total_memory) * 100
    
    def get_disk_usage(self):
        """
        Returns the disk usage as a percentage of total disk space.
        """
        try:
            total, used, _ = shutil.disk_usage("/")
            usage_percent = (used / total) * 100 if total else 0
            return usage_percent
        except Exception as e:
            logging.error(f"Error getting disk usage: {e}")
            return None


class PowerMonitor:
    """
    Class responsible for monitoring the power status of a Raspberry Pi by checking for under-voltage and other power-related issues
    """
    def __init__(self):
        self.last_throttled = None

    def get_power_status(self):
        """
        Retrieves the current power status, including checking for throttling and
        under-voltage conditions
        """
        throttled_value = self.get_throttled_status()

        if throttled_value is None:
            return "Unable to determine power status"

        elif self.last_throttled is None:
            self.last_throttled = throttled_value
            return "Checking status"
        
        elif self.last_throttled != throttled_value:
            logging.info(f"Status changed: {throttled_value:#010x}")
            self.last_throttled = throttled_value

        current_issues = self.analyze_throttled(throttled_value & 0xffff)
        past_issues = self.analyze_throttled(throttled_value >> 16)
        combined_issues = current_issues + (["Past Issues:"] + past_issues if past_issues else [])
        if combined_issues:
            logging.info(f"Issues detected: {'; '.join(combined_issues)}")

        return "; ".join(current_issues) if current_issues else "OK"

    def get_throttled_status(self):
        """
        Executes the 'vcgencmd get_throttled' command to retrieve the throttled status from the Raspberry Pi's VideoCore GPU
        """
        try:
            result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True)
            if result.returncode == 0:
                return int(result.stdout.split('=')[1], 0)
            else:
                logging.error("Unable to determine power status")
                return None
        except Exception as e:
            logging.error(f"Error checking power status: {e}")
            return None

    def analyze_throttled(self, throttled_value):
        """
        Analyzes the bits of the throttled status to determine if there are any current or past power issues and prepares an error message for the OLED display
        """
        if throttled_value & 0x1:
            return "Undervolt"
        elif throttled_value & 0x2:
            return "Freq cap"
        elif throttled_value & 0x4:
            return "Throttled"
        elif throttled_value & 0x8:
            return "Temp limit"
        return "OK"


class OledDisplay:
    """
    A class to encapsulate the OLED display (luma.oled.device.sh1106) logic and drawing methods
    """
    TEXT_COLOR = "white"
    DISPLAY_WIDTH = 128
    DISPLAY_HEIGHT = 64
    BAR_WIDTH = 62
    BAR_HEIGHT = 7

    def __init__(self, port, address, rotate):
        """
        Initializes the display device
        """
        self.serial = i2c(port=port, address=address)
        self.device = sh1106(self.serial, rotate=rotate)

    def draw_progress_bar(self, draw, x, y, percentage):
        """
        Draws a progress bar on the OLED display
        """
        fill_width = int(self.BAR_WIDTH * percentage / 100)
        draw.rectangle((x, y, x + self.BAR_WIDTH, y + self.BAR_HEIGHT), outline=self.TEXT_COLOR, fill=None)
        draw.rectangle((x, y, x + fill_width, y + self.BAR_HEIGHT), outline=self.TEXT_COLOR, fill=self.TEXT_COLOR)

    def update_display(self, cpu_temp, cpu_usage, ram_usage, disk_usage, download_speed, power_status):
        """
        Updates the OLED display with the current system status
        """
        with canvas(self.device) as draw:
            self.draw_progress_bar(draw, 30, 2, cpu_usage)
            self.draw_progress_bar(draw, 30, 14, ram_usage)
            self.draw_progress_bar(draw, 30, 26, disk_usage)

            draw.text((0, 0), "CPU", fill=self.TEXT_COLOR)
            draw.text((100, 0), f"{cpu_usage:.0f} %", fill=self.TEXT_COLOR)
            draw.text((0, 12), "RAM", fill=self.TEXT_COLOR)
            draw.text((100, 12), f"{ram_usage:.0f} %", fill=self.TEXT_COLOR)
            draw.text((0, 24), "Disk", fill=self.TEXT_COLOR)
            draw.text((100, 24), f"{disk_usage:.0f} %", fill=self.TEXT_COLOR)
            draw.text((0, 39), f"DL:{download_speed:.0f} KB/s", fill=self.TEXT_COLOR)
            draw.text((81, 39), f"T:{cpu_temp:.1f} C", fill=self.TEXT_COLOR)
            draw.text((0, 54), f"Power: {power_status}", fill=self.TEXT_COLOR)


def raspberry_monitor():
    """
    The main loom monitoring loop hardware system that updates the display with system status
    """
    monitor = RaspberryMonitor()
    power_monitor = PowerMonitor()
    display = OledDisplay(port=1, address=0x3c, rotate=0)

    while True:
        start_time = time.time()
        cpu_temp = monitor.get_cpu_temp()
        cpu_usage = monitor.get_cpu_usage()
        ram_usage = monitor.get_ram_usage()
        disk_usage = monitor.get_disk_usage()
        download_speed, upload_speed = monitor.get_network_speed()
        throttled_value = power_monitor.get_throttled_status()
        power_status = power_monitor.analyze_throttled(throttled_value)

        display.update_display(cpu_temp, cpu_usage, ram_usage, disk_usage, download_speed, power_status)

        elapsed_time = time.time() - start_time
        time.sleep(max(0, 1 - elapsed_time))


if __name__ == "__main__":
    raspberry_monitor()

