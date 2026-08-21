# NIX based image builder

## Goal
This image builder will create Linux "appliance" operating system images
designed to run a complete AI software stack on a wide range of x86
(and future ARM) hardware.

### How the appliance works
The images are read-only SquashFS images that boot and run the AI appliance.
The machine will always have two images, A and B. If the machine is booted
from A, it can download new versions into the B partition (and vice versa).
Images are distributed with a matching vmlinuz, which is stored in a
special /boot partition that is always present (but not usually mounted)
on the appliance. While the images are distributed at SquashFS files, they
are installed into partitions using `dd` so that the Linux kernel can
boot directly from them without an initrd.

There will also be a user-data partition, which will use btrfs namespaces
to provide mounts on mutable paths, such as /etc and /var. It will also
store all user data, which will be in the form of Docker volumes.

Finally, there will be a control plane responsible for operating Docker
and providing a cohesive user interface. TBD on whether this control
plane will run in a Docker container or natively via systemd on the
appliance.

## How the image works
The image primarily runs systemd and docker. Docker uses host networking
to remove the complicated virtual networking component. Systemd should
be used to ensure Docker and all containers are operating correctly.

One of the containers will run [Lemonade AI](https://lemonade-server.ai/),
which is a simple controller for LLM and stable diffusion apps. This
container will require access to any GPU(s) detected in the system, and
will require NVIDIA and AMD container GPU toolkits and drivers (with
Intel ARC and ARM GPU added in the future).

Other containers will run apps on top of the LLM and diffusion engine,
such as open-webui. These containers are less likely to require GPU access,
but it is a possibility.

The image should dynamically load the appropriate support for all GPUs
that are discovered at boot time; we will not have separate images for
NVIDIA versus AMD, as some appliances may have both GPUs present. The
control plane will determine how to allocate GPU(s) to container(s)
via Docker Compose configuration.

## Image building

The images will be built from a NixOS flake configuration. This will produce
the SquashFS file and matching vmlinuz, which will have a version string
(e.g. "20260810.01") embedded in both so they can be easily identified.

In addition to the NixOS configuration, there should be a Makefile that
makes image building, versioning, etc simple and repeatable.

In the future, images and kernels must be signed in a way that can be
verified at boot time to prevent software supply chain attacks.
