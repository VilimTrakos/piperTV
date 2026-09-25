# PiperTV

Hi there! Thanks for checking out this repository! :)

I created **PiperTV** because I wanted to experiment with communicating with and extending a relatively closed TV system — in my case, a **Grundig TV running its own proprietary OS**.

Instead of replacing the TV itself, PiperTV uses a Raspberry Pi connected over HDMI together with an IR receiver. The goal is to make the Raspberry Pi feel like a natural extension of the TV while still allowing the original TV remote to control everything.

## Hardware

My current setup uses a **Raspberry Pi 3B+ with 1 GB of RAM**.

And yes... **1 GB of RAM is definitely not enough for everything.** :D

The system works surprisingly well, but it can become a little slow when using a full web browser. Streaming services such as **HBO Max** and **Prime Video** can take some time to load, but once a movie or episode starts playing, playback generally works fine.

For receiving signals from the original TV remote, I'm using a **TSOP22-series IR receiver**:

<img width="870" height="996" alt="TSOP22 IR receiver" src="https://github.com/user-attachments/assets/f83d61a1-cb7d-4962-a537-c691ccf724ba" />

The system is connected as shown below:

<img width="1800" height="1260" alt="rpi3b-tsop22-tv(1)" src="https://github.com/user-attachments/assets/625052ce-30c4-4fc6-92e4-4c2195782036" />

By default, PiperTV expects the IR receiver signal pin to be connected to **physical GPIO pin 11 (GPIO17)**.

However, this can be changed in the configuration, so you can use almost any suitable GPIO pin.

## Installation
**Note**: you need Raspberry Pi OS with the desktop, not Lite. The interface is a window on the Pi's screen, so there has to be a screen to put it on!

PiperTV is designed to run on Raspberry Pi OS.

First, clone the repository:

```bash
git clone https://github.com/VilimTrakos/piperTV.git
cd piperTV
```

Then make the installer executable and run it:

```bash
chmod +x install.sh
./install.sh
```

The installation script takes care of installing and configuring the dependencies required by PiperTV.

After the installation finishes, reboot the Raspberry Pi:

```bash
sudo reboot
```

Once the Raspberry Pi starts again, PiperTV should be ready for the initial setup.

## Using PiperTV

Open **`http://PI_ADDRESS:8765`** from any browser on your network. That is the
studio: record buttons, bind them to actions, and set how the cursor behaves.

On the Pi's own screen there is a **PiperTV icon** on the desktop and on the
panel. Opening it puts the TV interface on the television. The remote works
before that too: after a reboot it moves the desktop's mouse, and two presses
of OK on the icon start Piper.

## Remote setup

Before using PiperTV, you first need to teach it the IR signals produced by your remote control.

Open the PiperTV settings and start the **IR capture/setup process**.

For each button you want to use:

1. Select the button/action you want to configure.
2. Press the corresponding button on your TV remote.
3. PiperTV captures and stores the IR signal.
4. Assign the action that should be executed when that signal is detected.

You are not limited to the original function of a button — you can choose what each captured button should do inside PiperTV.

In my case, with a **Grundig TV**, I also had to select the Raspberry Pi as the active system device. To do this, I selected the Raspberry Pi as the source, opened **Tools**, scrolled to the bottom, and selected the Raspberry Pi as the connected device, as shown below:

<img width="510" height="703" alt="image" src="https://github.com/user-attachments/assets/66402131-78b3-4055-bf88-bca3e8645d7d" />

<img width="2040" height="1536" alt="image" src="https://github.com/user-attachments/assets/2393ad42-d67a-4b5a-854f-4ec815228fb2" />

<img width="921" height="540" alt="image" src="https://github.com/user-attachments/assets/fbaf08a4-bcf7-4e69-927c-eacf045b72d7" />


This step may be different depending on your TV manufacturer and model.

Once the remote setup is complete, you're ready to start using **PiperTV!**

The original TV remote can now control the Raspberry Pi interface, launch supported services, navigate the UI, and interact with applications without requiring a separate keyboard or mouse.

Note:
Control is deliberately hard to switch on by accident. In the studio, confirm
that the TV is showing the Pi and pick a mode for that visit — **Piper
interface**, **Pointer** or **Snapping**. If your TV cannot report its selected
input over HDMI-CEC (many cannot), the confirmation is the manual one, and it
is shown as manual rather than as evidence.

Note in a note:
Snapping is not that good, can't find a way to make it better currently



## Demo

You can see PiperTV in action in the YouTube demo below:

/<YOUTUBE_DEMO - in works :D/>

