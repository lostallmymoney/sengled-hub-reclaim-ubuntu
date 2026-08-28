/*
 * ezsp-listen-bridge-v3
 * Sengled Element Hub RTL8196E -> direct TCP EZSP/ASH bridge for ZHA.
 *
 * Listens directly on 0.0.0.0:6638.  ZHA/Bellows connects to:
 *     socket://<hub-ip>:6638
 *
 * UART side:
 *   /dev/ttyS1, 57600 8N1, no RTS/CTS
 *   sends one initial XON after each TCP client connects
 *   consumes UART XON/XOFF and pauses TCP->UART while XOFF is active
 *
 * Network side:
 *   old Linux socketcall ABI (RTL8196E vendor kernel)
 *   SO_REUSEADDR + TCP_NODELAY
 *   one client at a time; returns to accept() after disconnect
 *
 * NO GPIO / NO RESET / NO FLASH / NO Zigbee network changes.
 */

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef signed int     s32;

#define SYS_exit       4001
#define SYS_read       4003
#define SYS_write      4004
#define SYS_open       4005
#define SYS_close      4006
#define SYS_ioctl      4054
#define SYS_socketcall 4102
#define SYS_nanosleep  4166

#define O_RDWR       0x0002
#define O_NONBLOCK   0x0080
#define O_NOCTTY     0x0800

#define TCGETS      0x540d
#define TCSETS      0x540e
#define TCFLSH      0x5407
#define TCIOFLUSH   2
#define CBAUD       0x0000100fU
#define CSIZE       0x00000030U
#define CS8         0x00000030U
#define CSTOPB      0x00000040U
#define CREAD       0x00000080U
#define PARENB      0x00000100U
#define PARODD      0x00000200U
#define CLOCAL      0x00000800U
#define B57600      0x00001001U
#define CRTSCTS     0x80000000U
#define VMIN_I      4
#define VTIME_I     5

#define AF_INET       2
#define SOCK_STREAM   2
#define SOL_SOCKET    1
#define SO_REUSEADDR  2
#define IPPROTO_TCP   6
#define TCP_NODELAY   1

#define SC_SOCKET      1
#define SC_BIND        2
#define SC_LISTEN      4
#define SC_ACCEPT      5
#define SC_SEND        9
#define SC_RECV       10
#define SC_SETSOCKOPT 14

#define MSG_DONTWAIT 0x40

#define EINTR_NUM       4
#define EAGAIN_NUM     11
#define EPIPE_NUM      32
#define ECONNRESET_NUM 104

struct termios_big {
    u32 c_iflag, c_oflag, c_cflag, c_lflag;
    u8 c_line;
    u8 c_cc[63];
};
struct timespec32_min { s32 tv_sec; s32 tv_nsec; };
struct sockaddr_in_min {
    u16 sin_family;
    u16 sin_port;
    u32 sin_addr;
    u8 zero[8];
};

static struct termios_big g_tio;
static u8 urx[512], nrx[512], txbuf[512];

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

__attribute__((noinline)) static long sockcall2a(long op,long a,long b){
    long args[2]; args[0]=a; args[1]=b;
    return sc2(SYS_socketcall,op,(long)args);
}
__attribute__((noinline)) static long sockcall3a(long op,long a,long b,long c){
    long args[3]; args[0]=a; args[1]=b; args[2]=c;
    return sc2(SYS_socketcall,op,(long)args);
}
__attribute__((noinline)) static long sockcall4a(long op,long a,long b,long c,long d){
    long args[4]; args[0]=a; args[1]=b; args[2]=c; args[3]=d;
    return sc2(SYS_socketcall,op,(long)args);
}
__attribute__((noinline)) static long sockcall5a(long op,long a,long b,long c,long d,long e){
    long args[5]; args[0]=a; args[1]=b; args[2]=c; args[3]=d; args[4]=e;
    return sc2(SYS_socketcall,op,(long)args);
}

static u32 slen(const char *s){ u32 n=0; while(s[n]) n++; return n; }
static void out(const char *s){ (void)sc3(SYS_write,1,(long)s,slen(s)); }
static char hd(u8 n){ n&=15; return (char)(n<10 ? '0'+n : 'A'+n-10); }
static void out_hex32(u32 v){
    char b[8]; int i;
    for(i=0;i<8;i++) b[i]=hd((u8)(v >> (28-(i*4))));
    (void)sc3(SYS_write,1,(long)b,8);
}
static void out_err(const char *where,long r){
    out("ERROR: "); out(where); out(" errno=0x"); out_hex32((u32)(-r)); out("\n");
}
static void sleep20(void){
    struct timespec32_min t; t.tv_sec=0; t.tv_nsec=20000000;
    (void)sc2(SYS_nanosleep,(long)&t,0);
}
static void sleep100(void){
    struct timespec32_min t; t.tv_sec=0; t.tv_nsec=100000000;
    (void)sc2(SYS_nanosleep,(long)&t,0);
}

static int uart_cfg(int fd){
    long r=sc3(SYS_ioctl,fd,TCGETS,(long)&g_tio);
    if(r<0) return -1;
    g_tio.c_iflag=0; g_tio.c_oflag=0; g_tio.c_lflag=0;
    g_tio.c_cflag &= ~(CBAUD|CSIZE|CSTOPB|PARENB|PARODD|CRTSCTS);
    g_tio.c_cflag |= (B57600|CS8|CREAD|CLOCAL);
    g_tio.c_cc[VMIN_I]=0; g_tio.c_cc[VTIME_I]=1;
    r=sc3(SYS_ioctl,fd,TCSETS,(long)&g_tio);
    return r<0 ? -1 : 0;
}

static long net_send(int s,const u8 *p,u32 n){
    u32 o=0;
    while(o<n){
        long r=sockcall4a(SC_SEND,s,(long)(p+o),n-o,0);
        if(r>0){ o+=(u32)r; continue; }
        if(r<0 && (((-r)==EINTR_NUM)||((-r)==EAGAIN_NUM))){ sleep20(); continue; }
        return r;
    }
    return (long)o;
}
static long uart_send(int fd,const u8 *p,u32 n){
    u32 o=0;
    while(o<n){
        long r=sc3(SYS_write,fd,(long)(p+o),n-o);
        if(r>0){ o+=(u32)r; continue; }
        if(r<0 && (((-r)==EINTR_NUM)||((-r)==EAGAIN_NUM))){ sleep20(); continue; }
        return r;
    }
    return (long)o;
}

static int make_listener(void){
    struct sockaddr_in_min sa;
    long s,r;
    int one=1;
    u32 i;

    out("NET: socket()...\n");
    s=sockcall3a(SC_SOCKET,AF_INET,SOCK_STREAM,0);
    if(s<0){ out_err("socket",s); return -1; }
    out("NET: socket() OK\n");

    out("NET: setsockopt(SO_REUSEADDR)...\n");
    r=sockcall5a(SC_SETSOCKOPT,s,SOL_SOCKET,SO_REUSEADDR,(long)&one,(long)sizeof(one));
    if(r<0){ out_err("setsockopt(SO_REUSEADDR) nonfatal",r); } else { out("NET: SO_REUSEADDR OK\n"); }

    sa.sin_family=AF_INET;
    sa.sin_port=(u16)6638;   /* big-endian CPU => bytes 19 EE on wire */
    sa.sin_addr=0;           /* INADDR_ANY / 0.0.0.0 */
    for(i=0;i<8;i++) sa.zero[i]=0;

    out("NET: bind(0.0.0.0:6638)...\n");
    r=sockcall3a(SC_BIND,s,(long)&sa,16);
    if(r<0){ out_err("bind(0.0.0.0:6638)",r); (void)sc1(SYS_close,s); return -1; }
    out("NET: bind() OK\n");

    out("NET: listen()...\n");
    r=sockcall2a(SC_LISTEN,s,1);
    if(r<0){ out_err("listen",r); (void)sc1(SYS_close,s); return -1; }
    out("NET: listen() OK\n");

    return (int)s;
}

static int accept_client(int listener){
    long c,r;
    int one=1;
    for(;;){
        c=sockcall3a(SC_ACCEPT,listener,0,0);
        if(c>=0) break;
        if((-c)==EINTR_NUM) continue;
        out_err("accept",c);
        sleep100();
    }
    r=sockcall5a(SC_SETSOCKOPT,c,IPPROTO_TCP,TCP_NODELAY,(long)&one,(long)sizeof(one));
    if(r<0){
        /* Non-fatal: bridge still works without Nagle disabled. */
        out_err("setsockopt(TCP_NODELAY) nonfatal",r);
    }
    return (int)c;
}

static int relay(int uart,int sock){
    int paused=0;
    u32 idle=0;
    static const u8 xon=0x11;

    if(uart_send(uart,&xon,1)!=1) return -1;

    for(;;){
        long n;
        u32 i,o;
        int did=0;

        n=sc3(SYS_read,uart,(long)urx,sizeof(urx));
        if(n>0){
            did=1; o=0;
            for(i=0;i<(u32)n;i++){
                if(urx[i]==0x13){ paused=1; continue; }
                if(urx[i]==0x11){ paused=0; continue; }
                txbuf[o++]=urx[i];
            }
            if(o && net_send(sock,txbuf,o)!=(long)o) return -1;
        } else if(n<0 && (-n)!=EAGAIN_NUM && (-n)!=EINTR_NUM){
            return -1;
        }

        n=sockcall4a(SC_RECV,sock,(long)nrx,sizeof(nrx),MSG_DONTWAIT);
        if(n>0){
            did=1;
            if(!paused){
                if(uart_send(uart,nrx,(u32)n)!=(long)n) return -1;
            } else {
                /* Preserve the TCP burst until the EM357 sends XON. */
                while(paused){
                    long u=sc3(SYS_read,uart,(long)urx,sizeof(urx));
                    if(u>0){
                        u32 j,z=0;
                        for(j=0;j<(u32)u;j++){
                            if(urx[j]==0x13){ paused=1; continue; }
                            if(urx[j]==0x11){ paused=0; continue; }
                            txbuf[z++]=urx[j];
                        }
                        if(z && net_send(sock,txbuf,z)!=(long)z) return -1;
                    } else if(u<0 && (-u)!=EAGAIN_NUM && (-u)!=EINTR_NUM){
                        return -1;
                    }
                    if(paused) sleep20();
                }
                if(uart_send(uart,nrx,(u32)n)!=(long)n) return -1;
            }
        } else if(n==0){
            return 0;
        } else if((-n)!=EAGAIN_NUM && (-n)!=EINTR_NUM){
            return -1;
        }

        if(!did){
            idle++;
            if(idle>=5){ sleep20(); idle=0; }
        } else {
            idle=0;
        }
    }
}

static int app(void){
    static char tty[]="/dev/ttyS1";
    long u;
    int listener,client;

    out("ezsp-listen-bridge-v3: direct ZHA TCP bridge\n");
    out("UART: /dev/ttyS1 57600 8N1\n");
    out("TCP : 0.0.0.0:6638\n");
    out("ZHA : socket://<hub-ip>:6638\n");
    out("NO GPIO / NO RESET / NO FLASH / NO network formation\n");

    u=sc3(SYS_open,(long)tty,O_RDWR|O_NOCTTY|O_NONBLOCK,0);
    if(u<0){ out_err("open(/dev/ttyS1)",u); return 3; }
    if(uart_cfg((int)u)<0){ out("ERROR: UART config failed\n"); (void)sc1(SYS_close,u); return 4; }
    (void)sc3(SYS_ioctl,u,TCFLSH,TCIOFLUSH);

    listener=make_listener();
    if(listener<0){ (void)sc1(SYS_close,u); return 5; }

    out("LISTENING: TCP/6638 ready\n");
    for(;;){
        out("waiting for ZHA/Bellows client...\n");
        client=accept_client(listener);
        if(client<0) continue;
        out("client connected; relaying EZSP/ASH\n");
        (void)relay((int)u,client);
        (void)sc1(SYS_close,client);
        out("client disconnected; returning to listen\n");
    }
}

__attribute__((noreturn,used)) void _start(void){
    int rc=app();
    (void)sc1(SYS_exit,rc);
    for(;;){}
}
