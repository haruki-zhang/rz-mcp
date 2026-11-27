# Prebuilt image

<!-- Which image, for MACHINE = "smarc-rzg3l", built from which layers and options.
Write it at step 5 (delivery); poc_package puts the image files of the last build here. -->

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
