sudo rm -rf PiZero_smallCyberdeck
sudo git clone https://github.com/grimmorium/PiZero_smallCyberdeck.git
sudo rm -rf PyAppsRunner
sudo git clone https://github.com/grimmorium/PyAppsRunner.git
cp PiZero_smallCyberdeck/refreshCyberdeck.sh refreshCyberdeck.sh
sudo chmod +x refreshCyberdeck.sh

cp PiZero_smallCyberdeck/runMenu.sh runMenu.sh
sudo chmod +x runMenu.sh

cp PiZero_smallCyberdeck/network.sh network.sh
sudo chmod +x network.sh

sudo rm -f PyAppsRunner/menu.xml
sudo cp PiZero_smallCyberdeck/menu.xml PyAppsRunner
sudo cp PiZero_smallCyberdeck/joystick_menuItem.py PyAppsRunner/scripts
sudo cp PiZero_smallCyberdeck/joystick_net.py PyAppsRunner/scripts
sudo cp PiZero_smallCyberdeck/joystick_net_cam.py PyAppsRunner/scripts
sudo cp PiZero_smallCyberdeck/rover_config.ini PyAppsRunner/scripts

sudo mkdir PyAppsRunner/scripts/tests
sudo cp PiZero_smallCyberdeck/joystick_test.py PyAppsRunner/scripts/tests

sudo mkdir PyAppsRunner/scripts/tools
sudo cp PiZero_smallCyberdeck/UART_scanner.py PyAppsRunner/scripts/tools
sudo cp PiZero_smallCyberdeck/spi_devices_test.py PyAppsRunner/scripts/tools
sudo cp PiZero_smallCyberdeck/interfaces_config.py PyAppsRunner/scripts/tools
sudo cp PiZero_smallCyberdeck/interfaces_config.xml PyAppsRunner/scripts/tools



sudo usermod -aG dialout $USER

#sudo apt install python3-pip
#sudo pip install pyserial
sudo apt-get install python3-serial

sudo mkdir PyAppsRunner/scripts/GPS
sudo cp PiZero_smallCyberdeck/gps_monitor.py PyAppsRunner/scripts/GPS
sudo cp PiZero_smallCyberdeck/interfaces_config.py PyAppsRunner/scripts/GPS
sudo cp PiZero_smallCyberdeck/interfaces_config.xml PyAppsRunner/scripts/GPS
sudo mkdir PyAppsRunner/scripts/NRF24
sudo cp PiZero_smallCyberdeck/nrf24_spectrum.py PyAppsRunner/scripts/NRF24
sudo cp PiZero_smallCyberdeck/interfaces_config.py PyAppsRunner/scripts/NRF24
sudo cp PiZero_smallCyberdeck/interfaces_config.xml PyAppsRunner/scripts/NRF24

sudo ls -la /boot/firmware

sudo rm -rf /boot/firmware/config.txt
sudo cp PiZero_smallCyberdeck/config.txt /boot/firmware/
#sudo rm -rf /boot/firmware/cmdline.txt
#sudo cp PiZero_smallCyberdeck/cmdline.txt /boot/firmware/
#sudo chmod 777 /boot/firmware/config.txt
#sudo chmod 777 /boot/firmware/cmdline.txt

sudo ls -la /boot/firmware

