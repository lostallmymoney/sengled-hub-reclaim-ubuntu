/*
 * em357-flash-v641-public-v1
 * Sengled Element Hub onboard EM357 -> EmberZNet 6.4.1 / EZSP v7 NCP.
 *
 * Input: /tmp/em357-v641-ncp-uart-sw.ebl (exactly 146816 bytes)
 * UART bootloader: /dev/ttyS1, 115200 8N1, XMODEM-CRC/128
 * GPIO13: BOOTLOAD (active low), GPIO11: RESET (active low)
 *
 * Requires /tmp/FLASH_EM357_NOW whose first 3 bytes are "YES".
 * Destructive to the EM357 application once block 1 is ACKed.
 * Does not erase SimEE explicitly and does not form/change a Zigbee network.
 */

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef signed int     s32;

#define SYS_exit      4001
#define SYS_read      4003
#define SYS_write     4004
#define SYS_open      4005
#define SYS_close     4006
#define SYS_ioctl     4054
#define SYS_nanosleep 4166
#define SYS_poll      4188

#define O_RDONLY     0x0000
#define O_WRONLY     0x0001
#define O_RDWR       0x0002
#define O_NONBLOCK   0x0080
#define O_NOCTTY     0x0800

#define TCGETS    0x540d
#define TCSETS    0x540e
#define TCFLSH    0x5407
#define TCIFLUSH  0

#define CBAUD   0x0000100fU
#define CSIZE   0x00000030U
#define CS8     0x00000030U
#define CSTOPB  0x00000040U
#define CREAD   0x00000080U
#define PARENB  0x00000100U
#define PARODD  0x00000200U
#define CLOCAL  0x00000800U
#define B115200 0x00001002U
#define CRTSCTS 0x80000000U
#define VMIN_I  4
#define VTIME_I 5

#define POLLIN   0x0001
#define POLLERR  0x0008
#define POLLHUP  0x0010
#define POLLNVAL 0x0020

#define SOH    0x01
#define EOT    0x04
#define ACK    0x06
#define NAK    0x15
#define CAN    0x18
#define CRCCHR 0x43

#define IMAGE_SIZE   146816U
#define BLOCK_SIZE   128U
#define TOTAL_BLOCKS 1147U
#define RETRIES      10
#define EINTR_NUM    4
#define EAGAIN_NUM   11

struct termios_big { u32 c_iflag,c_oflag,c_cflag,c_lflag; u8 c_line; u8 c_cc[63]; };
struct timespec32_min { s32 tv_sec; s32 tv_nsec; };
struct pollfd_min { s32 fd; signed short events; signed short revents; };

static struct termios_big g_tio;
static struct pollfd_min g_pfd;
static u8 g_rx[64];
static u8 g_gpio_cmd[64];
static u8 g_packet[133];
static u8 g_block[128];

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

static u32 slen(const char*s){u32 n=0;while(s[n])n++;return n;}
static void sleep_ms(u32 ms){struct timespec32_min t;t.tv_sec=(s32)(ms/1000U);t.tv_nsec=(s32)((ms%1000U)*1000000U);(void)sc2(SYS_nanosleep,(long)&t,0);}
static long wr(int fd,const void*p,u32 n){
    const u8*b=(const u8*)p;u32 off=0;int transient=0;
    while(off<n){
        long r=sc3(SYS_write,fd,(long)(b+off),n-off);
        if(r>0){off+=(u32)r;transient=0;continue;}
        if(r<0&&(((-r)==EINTR_NUM)||((-r)==EAGAIN_NUM))&&transient++<100){sleep_ms(2);continue;}
        return r;
    }
    return (long)off;
}
static void out(const char*s){(void)wr(1,s,slen(s));}
static void err(const char*s){(void)wr(2,s,slen(s));}
static void out_u32(u32 v){char b[11];u32 i=0,j;if(v==0){(void)wr(1,"0",1);return;}while(v&&i<10){b[i++]=(char)('0'+v%10U);v/=10U;}for(j=0;j<i/2U;j++){char t=b[j];b[j]=b[i-1U-j];b[i-1U-j]=t;}(void)wr(1,b,i);}
static char hd(u8 n){n&=15;return(char)(n<10?'0'+n:'A'+n-10);}
static void out_hex8(u8 v){char b[2];b[0]=hd((u8)(v>>4));b[1]=hd(v);(void)wr(1,b,2);}

static long read_retry(int fd,void*p,u32 n){int tries=0;for(;;){long r=sc3(SYS_read,fd,(long)p,n);if(r>=0)return r;if((-r)==EINTR_NUM&&tries++<20)continue;if((-r)==EAGAIN_NUM&&tries++<20){sleep_ms(10);continue;}return r;}}

/* Vendor procfs expects the userspace command buffer to be writable. */
static int gpio(const char*cmd){
    static char path[]="/proc/gpio_ctrl";u32 n=slen(cmd),i;long fd,r;
    if(n>=sizeof(g_gpio_cmd))return-1;
    for(i=0;i<n;i++)g_gpio_cmd[i]=(u8)cmd[i];g_gpio_cmd[n]=0;
    fd=sc3(SYS_open,(long)path,O_WRONLY,0);if(fd<0)return-1;
    r=sc3(SYS_write,fd,(long)g_gpio_cmd,n);(void)sc1(SYS_close,fd);
    return r<0?-1:0;
}
static void restore_high(void){(void)gpio("set 13 1\n");(void)gpio("set 11 1\n");}
/*
 * After the receiver ACKs XMODEM EOT, the legacy EM35x bootloader still needs
 * time to finish validating/committing the EBL.  Resetting after only 100 ms
 * can interrupt that finalization and leave no runnable application.  Keep
 * BOOTLOAD inactive and allow five seconds before pulsing RESET.
 */
static void reset_to_app(void){(void)gpio("set 13 1\n");sleep_ms(5000);(void)gpio("set 11 0\n");sleep_ms(150);(void)gpio("set 11 1\n");}

static int uart_config(int fd){
    long r=sc3(SYS_ioctl,fd,TCGETS,(long)&g_tio);if(r<0)return-1;
    g_tio.c_iflag=0;g_tio.c_oflag=0;g_tio.c_lflag=0;
    g_tio.c_cflag&=~(CBAUD|CSIZE|CSTOPB|PARENB|PARODD|CRTSCTS);
    g_tio.c_cflag|=(B115200|CS8|CREAD|CLOCAL);
    g_tio.c_cc[VMIN_I]=0;g_tio.c_cc[VTIME_I]=1;
    (void)sc3(SYS_ioctl,fd,TCFLSH,TCIFLUSH);
    r=sc3(SYS_ioctl,fd,TCSETS,(long)&g_tio);return r<0?-1:0;
}
static int poll_rx(int fd,int ms){long r;g_pfd.fd=fd;g_pfd.events=POLLIN;g_pfd.revents=0;r=sc3(SYS_poll,(long)&g_pfd,1,ms);if(r<0)return-1;if(r==0)return 0;if(g_pfd.revents&(POLLERR|POLLHUP|POLLNVAL))return-2;return(g_pfd.revents&POLLIN)?1:0;}

/* Returns interesting byte, 0 on timeout, -1 on I/O error. */
static int wait_ctl(int fd,u32 timeout_ms){
    u32 elapsed=0;
    while(elapsed<timeout_ms){
        u32 rem=timeout_ms-elapsed;int slice=rem>25U?25:(int)rem;int p=poll_rx(fd,slice);long n,i;elapsed+=(u32)slice;
        if(p<0)return-1;if(p!=1)continue;
        n=sc3(SYS_read,fd,(long)g_rx,sizeof(g_rx));if(n<0){if((-n)==EAGAIN_NUM||(-n)==EINTR_NUM)continue;return-1;}
        for(i=0;i<n;i++)if(g_rx[i]==CAN)return CAN;
        for(i=0;i<n;i++)if(g_rx[i]==ACK)return ACK;
        for(i=0;i<n;i++)if(g_rx[i]==NAK)return NAK;
        for(i=0;i<n;i++)if(g_rx[i]==CRCCHR)return CRCCHR;
    }
    return 0;
}
static u16 crc16_xmodem(const u8*p,u32 n){u16 crc=0;u32 i;int j;for(i=0;i<n;i++){crc=(u16)(crc^((u16)p[i]<<8));for(j=0;j<8;j++)crc=(u16)((crc&0x8000U)?((crc<<1)^0x1021U):(crc<<1));}return crc;}

static int armed(void){
    static char path[]="/tmp/FLASH_EM357_NOW";u8 b[4];long fd,n;
    fd=sc3(SYS_open,(long)path,O_RDONLY,0);if(fd<0)return 0;n=sc3(SYS_read,fd,(long)b,sizeof(b));(void)sc1(SYS_close,fd);
    return n>=3&&b[0]=='Y'&&b[1]=='E'&&b[2]=='S';
}
static int check_image(void){
    static char path[]="/tmp/em357-v641-ncp-uart-sw.ebl";long fd,n;u32 total=0,reads=0;
    fd=sc3(SYS_open,(long)path,O_RDONLY,0);if(fd<0){err("ERROR: firmware open failed\n");return-1;}
    for(;;){n=read_retry((int)fd,g_block,BLOCK_SIZE);if(n<0){err("ERROR: firmware read failed during preflight\n");(void)sc1(SYS_close,fd);return-1;}if(n==0)break;reads++;total+=(u32)n;if(total>IMAGE_SIZE){err("ERROR: firmware larger than expected\n");(void)sc1(SYS_close,fd);return-1;}}
    (void)sc1(SYS_close,fd);
    if(total!=IMAGE_SIZE){err("ERROR: firmware size mismatch\n");return-1;}
    if(reads!=TOTAL_BLOCKS){err("ERROR: firmware was not read as 1147 x 128-byte blocks\n");return-1;}
    out("firmware preflight OK: 146816 bytes / 1147 blocks\n");return 0;
}
static int enter_bootloader(int fd){
    static const u8 cr=0x0d;int c;
    out("entering EM357 bootloader: GPIO13=BOOTLOAD, GPIO11=RESET\n");
    if(gpio("config 13 w\n")<0||gpio("config 11 w\n")<0)return-1;
    restore_high();sleep_ms(250);
    if(gpio("set 13 0\n")<0)return-1;sleep_ms(100);
    if(gpio("set 11 0\n")<0)return-1;sleep_ms(120);
    if(gpio("set 11 1\n")<0)return-1;sleep_ms(350);
    if(gpio("set 13 1\n")<0)return-1;
    out("waiting for XMODEM 'C'...\n");c=wait_ctl(fd,1800);if(c==CRCCHR)return 0;if(c<0)return-1;
    out("no 'C' yet; sending one CR\n");if(wr(fd,&cr,1)!=1)return-1;
    for(;;){c=wait_ctl(fd,3000);if(c==CRCCHR)return 0;if(c==CAN){err("ERROR: bootloader sent CAN\n");return-1;}if(c<=0)break;}
    return-1;
}
static int send_block(int uart,u8 blk,const u8*data){
    u16 crc;int attempt,c;u32 i;
    g_packet[0]=SOH;g_packet[1]=blk;g_packet[2]=(u8)(0xffU-blk);for(i=0;i<128U;i++)g_packet[3+i]=data[i];
    crc=crc16_xmodem(data,128U);g_packet[131]=(u8)(crc>>8);g_packet[132]=(u8)crc;
    for(attempt=1;attempt<=RETRIES;attempt++){
        if(wr(uart,g_packet,133U)!=133){err("ERROR: UART write failed\n");return-1;}
        {int stale_c=0;for(;;){c=wait_ctl(uart,10000);if(c==ACK)return 0;if(c==NAK||c==0)break;if(c==CAN){err("ERROR: bootloader cancelled transfer\n");return-1;}if(c<0){err("ERROR: UART receive failure\n");return-1;}if(c==CRCCHR){if(++stale_c>=8)break;continue;}}}
        out("retry block ");out_u32((u32)blk);out(" attempt ");out_u32((u32)(attempt+1));out("\n");
    }
    return-1;
}
static int upload(int uart){
    static char path[]="/tmp/em357-v641-ncp-uart-sw.ebl";long fd,n;u32 block_index,got;u8 blk;
    fd=sc3(SYS_open,(long)path,O_RDONLY,0);if(fd<0)return-1;
    out("XMODEM-CRC upload starting: 1147 blocks\n");
    for(block_index=0;block_index<TOTAL_BLOCKS;block_index++){
        got=0;while(got<BLOCK_SIZE){n=read_retry((int)fd,(void*)(g_block+got),BLOCK_SIZE-got);if(n<=0){err("ERROR: unexpected EOF while uploading\n");(void)sc1(SYS_close,fd);return-1;}got+=(u32)n;}
        blk=(u8)((block_index+1U)&0xffU);if(send_block(uart,blk,g_block)<0){err("ERROR: XMODEM block failed\n");(void)sc1(SYS_close,fd);return-1;}
        if(block_index==0U||((block_index+1U)%32U)==0U||block_index+1U==TOTAL_BLOCKS){out("ACK block ");out_u32(block_index+1U);out("/");out_u32(TOTAL_BLOCKS);out(" (wire #");out_hex8(blk);out(")\n");}
    }
    n=sc3(SYS_read,fd,(long)g_block,1);(void)sc1(SYS_close,fd);if(n!=0){err("ERROR: firmware had unexpected trailing data\n");return-1;}return 0;
}
static int finish_xmodem(int uart){static const u8 eot=EOT;int attempt,c;out("all data ACKed; sending EOT\n");for(attempt=1;attempt<=RETRIES;attempt++){if(wr(uart,&eot,1)!=1)return-1;c=wait_ctl(uart,10000);if(c==ACK)return 0;if(c==CAN){err("ERROR: bootloader cancelled at EOT\n");return-1;}}return-1;}

static int app(void){
    static const char tty[]="/dev/ttyS1";long uart;int pre;
    out("em357-flash-v641-public-v1: EM357 -> EmberZNet 6.4.1 / EZSP v7\n");
    out("TARGET: /tmp/em357-v641-ncp-uart-sw.ebl\n");
    out("WARNING: existing EM357 application will be overwritten once XMODEM starts.\n");
    if(check_image()<0)return 2;
    if(!armed()){err("NOT ARMED: /tmp/FLASH_EM357_NOW must begin with YES\n");return 9;}
    out("ARM marker OK\n");
    uart=sc3(SYS_open,(long)tty,O_RDWR|O_NOCTTY|O_NONBLOCK,0);if(uart<0){err("ERROR: cannot open /dev/ttyS1\n");return 3;}
    if(uart_config((int)uart)<0){err("ERROR: cannot configure /dev/ttyS1\n");(void)sc1(SYS_close,uart);return 4;}
    pre=poll_rx((int)uart,50);if(pre<0){err("ERROR: poll preflight failed; no GPIO changes made\n");(void)sc1(SYS_close,uart);return 5;}
    out("UART/poll preflight OK\n");
    if(enter_bootloader((int)uart)<0){err("ERROR: did not obtain XMODEM C; restoring normal boot\n");reset_to_app();(void)sc1(SYS_close,uart);return 6;}
    out("bootloader ready: received C\n");
    if(upload((int)uart)<0){err("FLASH FAILED. Leaving bootloader available; DO NOT power-cycle until recovery is decided.\n");(void)gpio("set 13 1\n");(void)sc1(SYS_close,uart);return 7;}
    if(finish_xmodem((int)uart)<0){err("ERROR: all blocks sent but EOT was not ACKed. Leaving bootloader state.\n");(void)gpio("set 13 1\n");(void)sc1(SYS_close,uart);return 8;}
    out("XMODEM complete: EOT ACKed\n");out("hardware-resetting EM357 into new application...\n");reset_to_app();sleep_ms(1500);(void)sc1(SYS_close,uart);out("FLASH COMPLETE\n");return 0;
}
__attribute__((noreturn,used)) void _start(void){int rc=app();(void)sc1(SYS_exit,rc);for(;;){}}
