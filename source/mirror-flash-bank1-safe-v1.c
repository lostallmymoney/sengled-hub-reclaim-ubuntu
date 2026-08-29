/*
 * mirror-flash-bank1-safe-v1
 * Sengled Element Hub RTL8196E / Linux 2.6.x
 *
 * Mirror of bank2-safe-flash-v2-block for THIS hub (10.42.0.119),
 * whose bank layout is INVERTED vs the upstream tool's assumption:
 *
 *   Running/protected = Bank2 (mtd2 kernel + mtd3 rootfs), /proc/bootbank=2
 *   Idle/target        = Bank1 (mtd0 kernel + mtd1 rootfs)
 *
 * We write reclaimed firmware into the IDLE Bank1 (mtd0/mtd1) while running
 * from the PROTECTED Bank2 — the exact safety invariant the upstream flasher
 * enforces, just with the roles mirrored.
 *
 * SAFETY (mirrors upstream):
 *   - Can write ONLY /dev/mtdblock0 or /dev/mtdblock1 (the idle Bank1).
 *   - REFUSES unless /proc/bootbank reports Bank 2 active (i.e., we are
 *     running from the protected bank, never overwriting the running bank).
 *   - Requires exactly ONE explicit sentinel file.
 *   - Requires exact source file size.
 *   - Checks block-device length before writing.
 *   - Writes complete partition, closes/syncs, then byte-for-byte verifies.
 *
 * The RTL819x mtdblock driver performs erase-before-write internally.
 */

typedef unsigned char u8;
typedef unsigned int u32;

#define SYS_exit   4001
#define SYS_read   4003
#define SYS_write  4004
#define SYS_open   4005
#define SYS_close  4006
#define SYS_lseek  4019
#define SYS_sync   4036

#define O_RDONLY 0x0000
#define O_RDWR   0x0002
#define SEEK_SET 0
#define SEEK_END 2

#define ROOTFS_SIZE 0x002D0000U
#define KERNEL_SIZE 0x00130000U
#define CHUNK 4096U

static u8 a[CHUNK], b[CHUNK];

__attribute__((noinline)) static long sc1(long nr,long x0){
    register long v0 __asm__("$2")=nr;
    register long a0 __asm__("$4")=x0;
    register long a3 __asm__("$7")=0;
    __asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a3)::"memory");
    return a3 ? -v0 : v0;
}
__attribute__((noinline)) static long sc2(long nr,long x0,long x1){
    register long v0 __asm__("$2")=nr;
    register long a0 __asm__("$4")=x0;
    register long a1 __asm__("$5")=x1;
    register long a3 __asm__("$7")=0;
    __asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a1),"+r"(a3)::"memory");
    return a3 ? -v0 : v0;
}
__attribute__((noinline)) static long sc3(long nr,long x0,long x1,long x2){
    register long v0 __asm__("$2")=nr;
    register long a0 __asm__("$4")=x0;
    register long a1 __asm__("$5")=x1;
    register long a2 __asm__("$6")=x2;
    register long a3 __asm__("$7")=0;
    __asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a1),"+r"(a2),"+r"(a3)::"memory");
    return a3 ? -v0 : v0;
}

static u32 slen(const char *s){u32 n=0;while(s[n])n++;return n;}
static void out(const char *s){(void)sc3(SYS_write,1,(long)s,slen(s));}
static char hd(u8 n){n&=15;return (char)(n<10?'0'+n:'A'+n-10);}
static void out_hex32(u32 v){char t[8];int i;for(i=0;i<8;i++)t[i]=hd((u8)(v>>(28-i*4)));(void)sc3(SYS_write,1,(long)t,8);}
static void err(const char *s,long r){out("ERROR: ");out(s);out(" errno=0x");out_hex32((u32)(-r));out("\n");}

static int exists(const char *p){
    long f=sc3(SYS_open,(long)p,O_RDONLY,0);
    if(f<0)return 0;
    (void)sc1(SYS_close,f);
    return 1;
}

static int running_bank2(void){
    static char p[]="/proc/bootbank";
    u8 v[8]; long f,n;
    f=sc3(SYS_open,(long)p,O_RDONLY,0);
    if(f<0){err("open(/proc/bootbank)",f);return 0;}
    n=sc3(SYS_read,f,(long)v,sizeof(v));
    (void)sc1(SYS_close,f);
    if(n<1){out("ERROR: could not read /proc/bootbank\n");return 0;}
    out("ACTIVE BOOT BANK REPORTS: ");
    (void)sc3(SYS_write,1,(long)v,n);
    if(v[0]!='2'){
        out("REFUSING: this mirror-flasher is only allowed while Bank 2 is active (running from the protected bank).\n");
        return 0;
    }
    return 1;
}

static int probe_block(const char *dev,u32 expected){
    long f,r;
    out("PROBE: ");out(dev);out(" ... ");
    f=sc3(SYS_open,(long)dev,O_RDONLY,0);
    if(f<0){out("\n");err("open(block)",f);return 0;}
    r=sc3(SYS_lseek,f,0,SEEK_END);
    (void)sc1(SYS_close,f);
    if(r<0){out("\n");err("lseek(block end)",r);return 0;}
    out("size=0x");out_hex32((u32)r);
    if((u32)r!=expected){
        out(" WRONG (expected 0x");out_hex32(expected);out(")\n");
        return 0;
    }
    out(" OK\n");
    return 1;
}

static long full_read(int fd,u8 *p,u32 n){
    u32 o=0;
    while(o<n){
        long r=sc3(SYS_read,fd,(long)(p+o),n-o);
        if(r>0){o+=(u32)r;continue;}
        if(r==0)return (long)o;
        return r;
    }
    return (long)o;
}
static long full_write(int fd,const u8 *p,u32 n){
    u32 o=0;
    while(o<n){
        long r=sc3(SYS_write,fd,(long)(p+o),n-o);
        if(r>0){o+=(u32)r;continue;}
        return r;
    }
    return (long)o;
}

static int flash_one(const char *src,const char *dev,u32 expected,const char *label){
    long sf=-1,mf=-1,r; u32 off=0,n,i;

    out("\nTARGET: ");out(label);
    out("\nSOURCE: ");out(src);
    out("\nDEVICE: ");out(dev);out("\n");

    sf=sc3(SYS_open,(long)src,O_RDONLY,0);
    if(sf<0){err("open(source)",sf);return 10;}

    r=sc3(SYS_lseek,sf,0,SEEK_END);
    if(r<0){err("lseek(source end)",r);(void)sc1(SYS_close,sf);return 11;}
    if((u32)r!=expected){
        out("ERROR: source size mismatch: got 0x");out_hex32((u32)r);
        out(" expected 0x");out_hex32(expected);out("\n");
        (void)sc1(SYS_close,sf);return 12;
    }
    r=sc3(SYS_lseek,sf,0,SEEK_SET);
    if(r<0){err("lseek(source start)",r);(void)sc1(SYS_close,sf);return 13;}

    mf=sc3(SYS_open,(long)dev,O_RDWR,0);
    if(mf<0){err("open(block for write)",mf);(void)sc1(SYS_close,sf);return 14;}

    r=sc3(SYS_lseek,mf,0,SEEK_END);
    if(r<0){err("lseek(block end)",r);goto fail;}
    if((u32)r!=expected){
        out("ERROR: block-device size mismatch: got 0x");out_hex32((u32)r);
        out(" expected 0x");out_hex32(expected);out("\n");
        goto fail;
    }
    r=sc3(SYS_lseek,mf,0,SEEK_SET);
    if(r<0){err("lseek(block start)",r);goto fail;}

    out("WRITING THROUGH MTD BLOCK DRIVER...");
    off=0;
    while(off<expected){
        n=expected-off; if(n>CHUNK)n=CHUNK;
        r=full_read((int)sf,a,n);
        if(r!=(long)n){out("\nERROR: source read failed/short\n");goto fail;}
        r=full_write((int)mf,a,n);
        if(r!=(long)n){
            out("\n");
            if(r<0)err("block write",r);else out("ERROR: short block write\n");
            goto fail;
        }
        off+=n;
        if((off & 0x3FFFFU)==0 || off==expected){out(" 0x");out_hex32(off);}
    }
    out(" done\nFLUSHING...");
    (void)sc1(SYS_sync,0);
    (void)sc1(SYS_close,mf); mf=-1;
    (void)sc1(SYS_close,sf); sf=-1;
    (void)sc1(SYS_sync,0);
    out(" done\n");

    out("VERIFYING BYTE-FOR-BYTE...");
    sf=sc3(SYS_open,(long)src,O_RDONLY,0);
    if(sf<0){err("reopen(source)",sf);return 16;}
    mf=sc3(SYS_open,(long)dev,O_RDONLY,0);
    if(mf<0){err("reopen(block)",mf);(void)sc1(SYS_close,sf);return 17;}

    off=0;
    while(off<expected){
        n=expected-off;if(n>CHUNK)n=CHUNK;
        r=full_read((int)sf,a,n);
        if(r!=(long)n){out("\nERROR: verify source read failed\n");goto failverify;}
        r=full_read((int)mf,b,n);
        if(r!=(long)n){out("\nERROR: verify block read failed\n");goto failverify;}
        for(i=0;i<n;i++){
            if(a[i]!=b[i]){
                out("\nERROR: VERIFY MISMATCH at offset 0x");out_hex32(off+i);out("\n");
                goto failverify;
            }
        }
        off+=n;
        if((off & 0x3FFFFU)==0 || off==expected){out(" 0x");out_hex32(off);}
    }
    out(" done\nVERIFY: PASS\n");
    (void)sc1(SYS_close,mf);
    (void)sc1(SYS_close,sf);
    return 0;

failverify:
    if(mf>=0)(void)sc1(SYS_close,mf);
    if(sf>=0)(void)sc1(SYS_close,sf);
    return 18;
fail:
    if(mf>=0)(void)sc1(SYS_close,mf);
    if(sf>=0)(void)sc1(SYS_close,sf);
    return 15;
}

static int app(void){
    static char sr[]="/tmp/FLASH_BANK1_ROOTFS_NOW";
    static char sk[]="/tmp/FLASH_BANK1_KERNEL_NOW";
    static char fr[]="/tmp/mtd1-bank1-rootfs-reclaimed.bin";
    static char fk[]="/tmp/mtd0-bank1-kernel-reclaimed.bin";
    static char dr[]="/dev/mtdblock1";
    static char dk[]="/dev/mtdblock0";
    int rmode,kmode;

    out("mirror-flash-bank1-safe-v1\n");
    out("Writes ONLY inactive Bank 1 through the RTL mtdblock driver (running from protected Bank 2).\n");

    if(!running_bank2())return 2;

    if(!probe_block(dk,KERNEL_SIZE) || !probe_block(dr,ROOTFS_SIZE)){
        out("REFUSING: block-device probe failed.\n");
        return 4;
    }

    rmode=exists(sr);
    kmode=exists(sk);
    if(rmode==kmode){
        out("REFUSING: create EXACTLY ONE sentinel:\n");
        out("  /tmp/FLASH_BANK1_ROOTFS_NOW\n");
        out("  /tmp/FLASH_BANK1_KERNEL_NOW\n");
        return 3;
    }

    if(rmode)return flash_one(fr,dr,ROOTFS_SIZE,"BANK1 ROOTFS / mtdblock1");
    return flash_one(fk,dk,KERNEL_SIZE,"BANK1 KERNEL / mtdblock0");
}

__attribute__((noreturn,used)) void _start(void){
    int rc=app();
    (void)sc1(SYS_exit,rc);
    for(;;){}
}
