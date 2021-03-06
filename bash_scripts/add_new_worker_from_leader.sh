#!/bin/bash
# first argument is the new name of the pioreactor, to replace raspberrypi

# remove from known_hosts if already present
ssh-keygen -R $1.local                                                             >/dev/null 2>&1
ssh-keygen -R $1                                                                   >/dev/null 2>&1
ssh-keygen -R raspberrypi.local                                                    >/dev/null 2>&1
ssh-keygen -R $(host raspberrypi.local | awk '/has address/ { print $4 ; exit }')  >/dev/null 2>&1
ssh-keygen -R $(host $1 | awk '/has address/ { print $4 ; exit }')                 >/dev/null 2>&1


# allow us to SSH in, but make sure we can first before continuing.
while ! sshpass -p 'raspberry' ssh -o StrictHostKeyChecking=no raspberrypi.local "true"
    do echo "SSH to raspberrypi.local missed - `date`"
    sleep 2
done

sshpass -p 'raspberry' ssh -o StrictHostKeyChecking=no raspberrypi.local 'mkdir -p .ssh'
cat ~/.ssh/id_rsa.pub | sshpass -p 'raspberry' ssh -o StrictHostKeyChecking=no raspberrypi.local 'cat >> .ssh/authorized_keys'

# install worker onto Rpi
ssh -o StrictHostKeyChecking=no raspberrypi.local "wget -O install_pioreactor_as_worker.sh https://gist.githubusercontent.com/CamDavidsonPilon/08aa165a283fb7af7262e4cb598bf6a9/raw/install_pioreactor_as_worker.sh && bash ./install_pioreactor_as_worker.sh $1"

touch /home/pi/.pioreactor/config_$1.ini
echo -e "# $1 specific configuration here overrides the configuration in config.ini" >> /home/pi/.pioreactor/config_$1.ini
echo -e "\n" >> /home/pi/.pioreactor/config_$1.ini
echo -e "[stirring]" >> /home/pi/.pioreactor/config_$1.ini
echo -e "duty_cycle_$1=0\n" >> /home/pi/.pioreactor/config_$1.ini
echo -e "[pump_calibration]" >> /home/pi/.pioreactor/config_$1.ini
crudini --set ~/.pioreactor/config.ini inventory $1 1


# more needs to happen after the worker is online again (it reboots)
while ! ping -c1 $1 &>/dev/null
    do echo "Ping missed - `date`"
    sleep 2
done
echo "Host $1 found - `date`"
sleep 1


# remove from known_hosts if already present idk...
ssh-keygen -R $1                                                     >/dev/null 2>&1
ssh-keygen -R $(host $1 | awk '/has address/ { print $4 ; exit }')   >/dev/null 2>&1

# add to known hosts
ssh-keyscan -H $1 >> ~/.ssh/known_hosts

# sync-configs
pios sync-configs --units $1

# techdebt
ssh -o StrictHostKeyChecking=no $1 'sudo rm -rf ~/pioreactor && git clone --depth 1 https://github.com/Pioreactor/pioreactor.git'

# reboot once more (previous reboot didn't have config.inis)
ssh -o StrictHostKeyChecking=no $1 'sudo reboot'
