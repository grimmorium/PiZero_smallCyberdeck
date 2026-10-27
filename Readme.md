run 
```bash
sudo raspi-config
```
and enable SPI


#enable I2C
#and disable loging in via UART 
#and enable hardware serial/UART



```bash
# Update and install git
sudo apt update
sudo apt install -y git
#sudo apt-get install --reinstall libraspberrypi0 libraspberrypi-dev libraspberrypi-doc libraspberrypi-bin

sudo git clone https://github.com/grimmorium/PiZero_smallCyberdeck.git
cp PiZero_smallCyberdeck/refreshCyberdeck.sh refreshCyberdeck.sh
sudo chmod +x refreshCyberdeck.sh
sudo ./refreshCyberdeck.sh
sudo chmod +x PiZero_smallCyberdeck/config.sh
sudo ./PiZero_smallCyberdeck/config.sh

# Clone the setup script repository
git clone https://github.com/grimmorium/ILI9488RPIScript_RaspberryPi_Zero.git

# Navigate to the script's directory
#cd ILI9488RPIScript_RaspberryPi_Zero

# Make the script executable and run it
chmod +x ILI9488RPIScript_RaspberryPi_Zero/setup_ili9488.sh
sudo ./ILI9488RPIScript_RaspberryPi_Zero/setup_ili9488.sh
```
