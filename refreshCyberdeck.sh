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
sudo mkdir PyAppsRunner/scripts/tests
sudo cp PiZero_smallCyberdeck/joystick_test.py PyAppsRunner/scripts/tests
