# Lemonseed: Home AI Appliance

Lemonseed provides an appliance image that enables you to run your own LLM and AI applications at your home or office, without sharing any of your data with the large AI or Cloud corporations. Lemonseed is open source, and is built with a number of open source projects including Lemonade Server, Open WebUI, Docker, NixOS, and many others. Open source software ensures that there are no back doors or secret exfiltration of your data.

## Getting Started

The Lemonseed install process is simple; create a USB stick which starts the Lemonseed Installer on the PC hardware of your choice. Lemonseed is compatible with a range of PC hardware, including GPUs from AMD and NVIDIA. Lemonseed does not support M-series Macintosh computers or ARM processor based PCs. See the Recommended Hardware section below for more information. Unlike some other image installers, the Lemonseed install process is purely web based; you might not need a keyboard + monitor attached to the PC that you plan to use for Lemonseed.

| Note: Lemonseed erases the operating system and all data that exists on the PC you use; do not run the Lemonseed installer on a PC that has data you want to preserve! |
| :---- |

### Step 1: Make a bootable USB stick

The Lemonseed installer is approximately 1.2GB in size; you will need a USB stick of at least 2GB capacity.

1. Download the Lemonseed installer image `lemonseed-installer.iso` from the [Releases](https://github.com/mattcorddry/lemonseed/releases) page.  
2. Install an app that can write ISO images to USB sticks. We recommend [Balena Etcher](https://etcher.balena.io/).  
3. Use Etcher or other application to write the downloaded `lemonseed-installer.iso` to the USB stick. Note: you cannot simply copy the `lemonseed-installer.iso` file to the USB stick – you must use an imaging application.

### Step 2: Boot the PC from the USB stick

1. With the PC turned off, insert the new USB stick into an empty USB port on the PC  
2. Power on the PC. If there is no operating system on the PC already, it will boot from the USB stick automatically.  
   * *Note: If there is already an operating system on the PC, you will need to force it to boot from the USB stick. Consult the instruction manual for your specific PC hardware model. [This video](https://www.youtube.com/watch?v=fNbyTs1Ev3I) provides a good overview if you have Windows 11 on the PC. This will require you to connect a monitor + keyboard to the PC.*

### Step 3: Install Lemonseed

1. The Lemonseed installer is web based. From a computer on your local network, connect to [`http://lemonseed.local`](http://lemonseed.local).  
2. The installer will prompt you with a few questions, then install Lemonseed onto the hard drive in your PC. It will prompt you to remove the USB stick and automatically reboot when finished.  
   * *Note: The Lemonseed appliance operating system is downloaded from the internet at this point; on a slow internet connection, the download may take awhile.*  
3. When Lemonseed boots for the first time, you will set up a name for the appliance, an administrator account and password, and other system settings. You will also select which Large Language Model (LLM) to download. The Lemonseed will complete setup and download the LLM, which can take awhile.

### Recommended hardware

Lemonseed runs on an ordinary Intel or AMD PC with a capable GPU. Lemonseed does not run on ARM-based PCs or Macintosh computers. The installer will check your hardware (disk, memory and GPU) and tell you if it does not meet Lemonseed’s requirements.

| Component | Minimum | Recommended | Status |
| --------- | ------- | ----------- | ------ |
| Processor | Intel 4+ core | Intel Core i3-10xxx or newer | Supported |
|           | AMD 4+ core | AMD Ryzen 5000 or newer | Supported |
|           | AMD Ryzen AI 385 APU | AMD Ryzen AI 395+ APU 64-128GB | Supported |
|           | ARM |       | Unsupported |
|           | Apple |     | Unsupported |
| Memory | 8GB | 16GB DDR4+ | Supported |
| Storage | 128GB SSD | 256GB+ NVMe SSD | Supported |
| GPU | NVIDIA GTX1660 6GB | NVIDIA RTX3080+ 16GB | Supported |
|     | AMD Radeon RX 6000 8GB | AMD Radeon RX 7000+ 16-32GB | Untested |
|     | Intel Iris Xe | Intel B60+ | Unsupported |
|     | ARM Neon      |            | Unsupported |

Other hardware requirements:
 * TPM v2 hardware key store is required for disk encryption (all recent PCs have this)
 * EFI boot (all recent PCs have this, but some may need it to be enabled in BIOS setup)
 * Ethernet network interface connected to your home network (WiFi is not yet supported)

## Using Lemonseed

Your Lemonseed is now running a Large Language Model and other AI services; it can be used for chat conversations, rendering and editing images, searching the internet for answers, etc. It will do most of what a public AI system (e.g. Google Gemini, ChatGPT) can do, but it will run slower and won’t match the intelligence level of the more advanced “Frontier” AI systems (e.g. GPT 6 or Claude Fable). This is simply due to the AI hardware; public AI runs in power-sucking datacenters on multimillion-dollar AI hardware.

### Connecting to Lemonseed

Lemonseed can be reached using the name you set during setup. For example, if you named your Lemonseed `Larry`, then you can reach the Lemonseed from your home network at [`http://larry.local`](http://larry.local). You may notice that your connection to the Lemonseed is not encrypted (the URL does not start with `https://`). This is because it is impossible to generate a trusted SSL certificate for a URL that ends in `.local`. To ensure that your password cannot be seen from other people on your network, Lemonseed uses an advanced login technique that never sends your password to the Lemonseed appliance.

Lemonseed does not yet support remote access; this is a capability we plan to release soon.

### Chat with Lemonseed

The default screen that appears when you log in to your Lemonseed is the open source [Open-WebUI](https://docs.openwebui.com/) chat interface, which provides a rich chat experience powered by the LLM that Lemonseed is running. Open-WebUI can also read files that you upload, search the web for answers to questions, and has basic speech capabilities. There are many more options in Open-WebUI – we recommend reading their documentation to learn more.

### Use Modules

Modules are optional add-on applications that run on the Lemonseed appliance alongside its core services. Open WebUI itself — the chat interface you use every day — is one such module, and the same mechanism will be used for future add-ons as the collection grows. The appliance pins each module to a specific, reviewed version, and modules are installed, updated and (if necessary) repaired for you by the Lemonseed software.

### Manage Settings

The gear icon in the upper right corner opens the Settings panel for your Lemonseed. In this panel you can change a range of settings, update the Lemonseed software when new releases are published, and change the Large Language Model that Lemonseed runs. The settings page has a number of panels:

* **General:** View and update the Lemonseed software, understand the current Lemonseed hardware and LLM health, and reboot or shut down the Lemonseed.  
* **AI Models:** Select the Large Language Model (LLM) that Lemonseed runs on your appliance. Lemonseed can only run one LLM at a time, but it can store multiple LLMs so that switching back and forth does not require repeated downloads. Lemonseed provides a short description of the strengths and origin of each LLM available.  
* **Users & Groups:** Manage users and groups on Lemonseed. This enables you to share Lemonseed with your family or friends and control the level of access each user has. This is also how you can set up content filters for more sensitive users.  
* **Open WebUI:** The Lemonseed control plane controls a few settings in Open WebUI to ensure that the two can communicate. If you are having problems with Open WebUI, you can force a resynchronization or reset settings here.  
* **Modules:** Lemonseed appliance capabilities can be expanded with the use of Modules, which can be configured in this panel.

### Manage Profile

Your user profile on Lemonseed can be updated by clicking on the User icon in the upper right corner. Clicking this icon enables you to log out, or to edit your profile. Currently the profile includes your name and password; more settings will be added as the Lemonseed project evolves.

## Contributing

We welcome contributions to this project! Please review [the documentation](./documentation/README.md) to learn more about how Lemonseed is built, and [our backlog](./documentation/roadmap/BACKLOG.md) for the work ahead of us.
