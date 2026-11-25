# Prebuilt image

<!-- Which image, for MACHINE = "smarc-rzg3l", built from which layers and options.
Filled in at Step 5 by the package step. -->

```plaintext
image/
├── <image>-smarc-rzg3l.rootfs.wic.gz
└── <image>-smarc-rzg3l.rootfs.wic.bmap
```

To write the image to a microSD card on the host PC:

```sh
# replace sdX with the actual microSD card device
sudo bmaptool copy <image>-smarc-rzg3l.rootfs.wic.gz /dev/sdX
```
