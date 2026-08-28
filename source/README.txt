Source for the custom hub-side binaries bundled in payload\.
The Windows controller source is Reclaim-SengledHub.ps1 + lib\ReclaimSupport.cs.
Third-party coordinator firmware is downloaded only when a coordinator flash is
needed. SquashFS utilities are downloaded during image building when they are not
already cached.
