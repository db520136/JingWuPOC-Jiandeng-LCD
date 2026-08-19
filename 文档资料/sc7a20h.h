/* SC7A20HA相关宏定义 */

#define SC7A20H_ADDR            0x19                        /* SDO引脚悬空/高电平时的地址，接地时为0x18 */
#define SC7A20H_ID              0x11                        /* 设备ID */
#define ONE_G                   9.807f                      /* 加速度单位转换使用 */
#define M_PI                    (3.14159265358979323846f)   /* 陀螺仪单位转换使用 */
#define MAX_CALI_COUNT          100                         /* 采样次数 */

/* 寄存器地址定义 */
#define SC7A20H_REG_CTRL0           0x1F        /* 控制寄存器0 */
#define SC7A20H_REG_CTRL1           0x20        /* 控制寄存器1 */
#define SC7A20H_REG_CTRL2           0x21        /* 控制寄存器2 */
#define SC7A20H_REG_CTRL3           0x22        /* 控制寄存器3 */
#define SC7A20H_REG_CTRL4           0x23        /* 控制寄存器4 */
#define SC7A20H_REG_CTRL5           0x24        /* 控制寄存器5 */
#define SC7A20H_REG_CTRL6           0x25        /* 控制寄存器6 */
#define SC7A20H_DRDY_STATUS_REG     0x27        /* 状态寄存器 */
#define SC7A20H_REG_OUT_X_L         0x28        /* X轴低字节 */
#define SC7A20H_REG_OUT_X_H         0x29        /* X轴高字节 */
#define SC7A20H_REG_OUT_Y_L         0x2A        /* Y轴低字节 */
#define SC7A20H_REG_OUT_Y_H         0x2B        /* Y轴高字节 */
#define SC7A20H_REG_OUT_Z_L         0x2C        /* Z轴低字节 */
#define SC7A20H_REG_OUT_Z_H         0x2D        /* Z轴高字节 */
#define SC7A20H_REG_WHO_AM_I        0x0F        /* 设备ID寄存器 */

/* 控制寄存器1 (0x20) 位定义 */
#define SC7A20H_ODR_1_56HZ          0x10        /* 1.56Hz输出数据率 */
#define SC7A20H_ODR_12_5HZ          0x20        /* 12.5Hz输出数据率 */
#define SC7A20H_ODR_25HZ            0x30        /* 25Hz输出数据率 */
#define SC7A20H_ODR_50HZ            0x40        /* 50Hz输出数据率 */
#define SC7A20H_ODR_100HZ           0x50        /* 100Hz输出数据率 */
#define SC7A20H_ODR_200HZ           0x60        /* 200Hz输出数据率 */
#define SC7A20H_ODR_400HZ           0x70        /* 400Hz输出数据率 */
#define SC7A20H_ODR_1_48KHZ         0x80        /* 1.48kHz输出数据率 */
#define SC7A20H_ODR_2_66KHZ         0x90        /* 2.66kHz输出数据率 */
#define SC7A20H_ODR_4_434KHZ        0xA0        /* 4.434kHz输出数据率 */
#define SC7A20H_LPEN                0x08        /* 低功耗模式使能 */
#define SC7A20H_ZEN                 0x04        /* Z轴使能 */
#define SC7A20H_YEN                 0x02        /* Y轴使能 */
#define SC7A20H_XEN                 0x01        /* X轴使能 */
#define SC7A20H_ENABLE_ALL_AXES     (SC7A20H_XEN | SC7A20H_YEN | SC7A20H_ZEN) // 使能所有轴

/* 控制寄存器4配置 */
#define SC7A20H_SCALE_2G            0x00        /* ±2G量程 */
#define SC7A20H_SCALE_4G            0x10        /* ±4G量程 */
#define SC7A20H_SCALE_8G            0x20        /* ±8G量程 */
#define SC7A20H_SCALE_16G           0x30        /* ±16G量程 */
#define SC7A20H_BDU_ENABLE          0x88        /* 块数据更新使能 */

/* 中断映射 */
#define SC7A20H_MAP_INT1            0x01
#define SC7A20H_MAP_INT2            0x02

typedef struct {
    uint8_t data[2];
    float  acc_x;
    float  acc_y;
    float  acc_z;
    float  acc_g;
    float  pitch;                       /* 围绕X轴旋转,也叫做俯仰角 */
    float  roll;                        /* 围绕Z轴旋转,也叫翻滚角 */
} sc7a20h_rawdata_t;

/* 函数声明 */
float sc7a20h_get_temperature(void);                                /* 获取传感器温度 */
uint8_t get_euler_angles(float *pitch, float *roll, float *yaw);    /* 获取欧拉角数据 */
void sc7a20h_read_xyz(float *acc, float *gyro);                     /* 获取加速度计和陀螺仪的三轴数据 */
void sc7a20h_read_rawdata(sc7a20h_rawdata_t *rawdata);              /* 读取原始数据 */
esp_err_t sc7a20h_init(void);