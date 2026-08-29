/*
 * em357-v641-live-probe-v1
 * Non-destructive EZSP VERSION probe for the Sengled onboard EM357.
 * NO GPIO, NO reset, NO bootloader, NO flash.
 * Opens /dev/ttyS1 at 57600 8N1, sends XON and one ASH DATA #0 EZSP VERSION.
 */
typedef unsigned char u8; typedef unsigned int u32; typedef signed int s32;
#define SYS_exit 4001
#define SYS_read 4003
#define SYS_write 4004
#define SYS_open 4005
#define SYS_close 4006
#define SYS_ioctl 4054
#define SYS_nanosleep 4166
#define SYS_poll 4188
#define O_RDWR 0x0002
#define O_NONBLOCK 0x0080
#define O_NOCTTY 0x0800
#define TCGETS 0x540d
#define TCSETS 0x540e
#define TCFLSH 0x5407
#define TCIOFLUSH 2
#define CBAUD 0x0000100fU
#define CSIZE 0x00000030U
#define CS8 0x00000030U
#define CSTOPB 0x00000040U
#define CREAD 0x00000080U
#define PARENB 0x00000100U
#define PARODD 0x00000200U
#define CLOCAL 0x00000800U
#define B57600 0x00001001U
#define CRTSCTS 0x80000000U
#define VMIN_I 4
#define VTIME_I 5
#define POLLIN 0x0001
#define POLLERR 0x0008
#define POLLHUP 0x0010
#define POLLNVAL 0x0020
#define EINTR_NUM 4
#define EAGAIN_NUM 11
struct termios_big {u32 c_iflag,c_oflag,c_cflag,c_lflag;u8 c_line;u8 c_cc[63];};
struct timespec32_min {s32 tv_sec;s32 tv_nsec;};
struct pollfd_min {s32 fd;signed short events;signed short revents;};
static struct termios_big g_tio;static struct pollfd_min g_pfd;static u8 g_rx[1024];
__attribute__((noinline)) static long sc1(long nr,long x0){register long v0 __asm__("$2")=nr,a0 __asm__("$4")=x0,a3 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a3)::"memory");return a3?-v0:v0;}
__attribute__((noinline)) static long sc2(long nr,long x0,long x1){register long v0 __asm__("$2")=nr,a0 __asm__("$4")=x0,a1 __asm__("$5")=x1,a3 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a1),"+r"(a3)::"memory");return a3?-v0:v0;}
__attribute__((noinline)) static long sc3(long nr,long x0,long x1,long x2){register long v0 __asm__("$2")=nr,a0 __asm__("$4")=x0,a1 __asm__("$5")=x1,a2 __asm__("$6")=x2,a3 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v0),"+r"(a0),"+r"(a1),"+r"(a2),"+r"(a3)::"memory");return a3?-v0:v0;}
static u32 slen(const char*s){u32 n=0;while(s[n])n++;return n;}static void sleep_ms(u32 ms){struct timespec32_min t;t.tv_sec=(s32)(ms/1000U);t.tv_nsec=(s32)((ms%1000U)*1000000U);(void)sc2(SYS_nanosleep,(long)&t,0);}static long wr(int fd,const void*p,u32 n){const u8*b=(const u8*)p;u32 o=0;int tr=0;while(o<n){long r=sc3(SYS_write,fd,(long)(b+o),n-o);if(r>0){o+=(u32)r;tr=0;continue;}if(r<0&&(((-r)==EINTR_NUM)||((-r)==EAGAIN_NUM))&&tr++<250){sleep_ms(2);continue;}return r;}return(long)o;}static void out(const char*s){(void)wr(1,s,slen(s));}
static char hd(u8 n){n&=15;return(char)(n<10?'0'+n:'A'+n-10);}static void out_hex8(u8 v){char b[2];b[0]=hd((u8)(v>>4));b[1]=hd(v);(void)wr(1,b,2);}static void dump(const u8*p,u32 n){u32 i;for(i=0;i<n;i++){if(i)(void)wr(1," ",1);out_hex8(p[i]);}(void)wr(1,"\n",1);}
static int uart_cfg(int fd){long r=sc3(SYS_ioctl,fd,TCGETS,(long)&g_tio);if(r<0)return-1;g_tio.c_iflag=0;g_tio.c_oflag=0;g_tio.c_lflag=0;g_tio.c_cflag&=~(CBAUD|CSIZE|CSTOPB|PARENB|PARODD|CRTSCTS);g_tio.c_cflag|=(B57600|CS8|CREAD|CLOCAL);g_tio.c_cc[VMIN_I]=0;g_tio.c_cc[VTIME_I]=1;r=sc3(SYS_ioctl,fd,TCSETS,(long)&g_tio);return r<0?-1:0;}
static int poll_rx(int fd,int ms){long r;g_pfd.fd=fd;g_pfd.events=POLLIN;g_pfd.revents=0;r=sc3(SYS_poll,(long)&g_pfd,1,ms);if(r<0)return-1;if(r==0)return 0;if(g_pfd.revents&(POLLERR|POLLHUP|POLLNVAL))return-2;return(g_pfd.revents&POLLIN)?1:0;}
static u32 collect(int fd,u32 timeout_ms,u32 quiet_ms){u32 total=0,elapsed=0,quiet=0;while(elapsed<timeout_ms&&total<sizeof(g_rx)){int p=poll_rx(fd,20);elapsed+=20U;if(p<0)break;if(p==0){if(total){quiet+=20U;if(quiet>=quiet_ms)break;}continue;}quiet=0;if(p==1){long n=sc3(SYS_read,fd,(long)(g_rx+total),sizeof(g_rx)-total);if(n>0)total+=(u32)n;else if(n<0&&(-n)!=EAGAIN_NUM&&(-n)!=EINTR_NUM)break;}}return total;}
static u8 whiten_next(u8 curr){return(u8)((curr&1U)?((curr>>1)^0xB8U):(curr>>1));}
static int decode_version(const u8*p,u32 n){u32 i,j;for(i=0;i+11U<=n;i++){if(p[i]!=0x01U||p[i+10U]!=0x7EU)continue;{u8 seq=0x42U,d[7];for(j=0;j<7U;j++){d[j]=(u8)(p[i+1U+j]^seq);seq=whiten_next(seq);}out("Decoded candidate: ");dump(d,7);if(d[0]==0x00U&&d[1]==0x80U&&d[2]==0x00U){out("EZSP protocolVersion=0x");out_hex8(d[3]);out(" stackType=0x");out_hex8(d[4]);out(" stackVersionRaw=0x");out_hex8(d[6]);out_hex8(d[5]);out("\n");return(int)d[3];}}}return-1;}
static int app(void){
 static char tty[]="/dev/ttyS1";static const u8 xon=0x11;static const u8 version_req[8]={0x00,0x42,0x21,0xA8,0x5C,0x2C,0xA0,0x7E};static const u8 ack[4]={0x81,0x60,0x59,0x7E};long fd;u32 n;int ver;
 out("em357-v641-live-probe-v1: non-destructive EZSP VERSION probe\n");out("NO GPIO / NO RESET / NO BOOTLOADER / NO FLASH\n");
 fd=sc3(SYS_open,(long)tty,O_RDWR|O_NOCTTY|O_NONBLOCK,0);if(fd<0){out("ERROR: cannot open /dev/ttyS1\n");return 3;}if(uart_cfg((int)fd)<0){out("ERROR: UART config failed\n");(void)sc1(SYS_close,fd);return 4;}
 (void)sc3(SYS_ioctl,fd,TCFLSH,TCIOFLUSH);sleep_ms(80);if(wr((int)fd,&xon,1)!=1){out("ERROR: XON write failed\n");(void)sc1(SYS_close,fd);return 5;}n=collect((int)fd,450U,160U);if(n){out("RX after XON: ");dump(g_rx,n);}else out("RX after XON: <none>\n");
 (void)sc3(SYS_ioctl,fd,TCFLSH,TCIOFLUSH);sleep_ms(60);out("TX EZSP VERSION\n");if(wr((int)fd,version_req,8)!=8){out("ERROR: VERSION write failed\n");(void)sc1(SYS_close,fd);return 6;}n=collect((int)fd,1800U,350U);out("RX: ");if(n)dump(g_rx,n);else out("<none>\n");
 if(!n){out("RESULT: NO EZSP VERSION RESPONSE\n");(void)sc1(SYS_close,fd);return 7;}ver=decode_version(g_rx,n);if(ver<0){out("RESULT: RESPONSE NOT DECODED AS EZSP VERSION\n");(void)sc1(SYS_close,fd);return 8;}(void)wr((int)fd,ack,4);if(ver==7){out("RESULT: EZSP_V7_OK\n");(void)sc1(SYS_close,fd);return 0;}out("RESULT: EZSP_VERSION_NOT_7\n");(void)sc1(SYS_close,fd);return 9;
}
__attribute__((noreturn,used)) void _start(void){int rc=app();(void)sc1(SYS_exit,rc);for(;;){}}
