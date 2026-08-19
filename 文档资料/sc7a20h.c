/* 全局变量缓存区 */
i2c_master_dev_handle_t sc7a20h_handle = NULL;
const char* sc7a20h_name = "sc7a20h"; 
#define M_G                         9.80665f
#define RAD_TO_DEG                  (180.0f / M_PI)                         /* 0.017453292519943295 */
#define SC7A20H_AUTO_INCREMENT      0x80

/**
 * @brief       读取sc7a20h寄存器的数据
 * @param       reg_addr       : 要读取的寄存器地址
 * @param       data           : 读取的数据
 * @param       len           : 数据大小
 * @retval      错误值        ：0成功，其他值：错误
 */
esp_err_t sc7a20h_register_read(const uint8_t reg, uint8_t *data, const size_t len)
{
    uint8_t reg_addr = reg;

    if (len > 1)
    {
        reg_addr |= SC7A20H_AUTO_INCREMENT;
    }

    return i2c_master_transmit_receive(sc7a20h_handle, &reg_addr, 1, data, len, -1);
}

/**
 * @brief       向sc7a20h寄存器写数据
 * @param       reg_addr       : 要写入的寄存器地址
 * @param       data           : 要写入的数据
 * @retval      错误值        ：0成功，其他值：错误
 */
static esp_err_t sc7a20h_register_write_byte(uint8_t reg, uint8_t data)
{
    esp_err_t ret;

    uint8_t *buf = malloc(2);
    if (buf == NULL)
    {
        ESP_LOGE(sc7a20h_name, "%s memory failed", __func__);
        return ESP_ERR_NO_MEM;
    }

    buf[0] = reg;
    buf[1] = data;

    ret = i2c_master_transmit(sc7a20h_handle, buf, 2, -1);

    free(buf);

    return ret;
}

/**
 * @brief 读取单轴12位加速度值（左对齐，带符号扩展）
 * @param lsb_addr: 低字节寄存器地址 (e.g., 0x28)
 * @param msb_addr: 高字节寄存器地址 (e.g., 0x29)
 * @retval int16_t: 原始12位有符号值（单位：LSB，±2048 对应 ±2G）
 */
static int16_t sc7a20h_read_axis_12bit(uint8_t lsb_addr, uint8_t msb_addr)
{
    uint8_t lsb, msb;
    esp_err_t ret;

    /* 先读低字节 */
    ret = sc7a20h_register_read(lsb_addr, &lsb, 1);
    if (ret != ESP_OK) 
    {
        return 0;
    }

    ret = sc7a20h_register_read(msb_addr, &msb, 1);

    if (ret != ESP_OK) return 0;

    uint16_t temp = ((uint16_t)msb << 8) | lsb;

    temp >>= 4;

    if (msb & 0x80) 
    {
        temp |= 0xF000;  /* 补高4位为1（16位补码） */
    } 
    else 
    {
        temp &= 0x0FFF;  /* 清高4位（确保非负） */
    }

    return (int16_t)temp;
}

uint8_t xyz_data[6] = {0};
short raw_data[3] = {0};
float accl_data[3];
float acc_normal;
float scale_factor = 0.001f;  /* 默认±2G量程，1mg/digit */

/**
 * @brief       读取三轴数据(原始数据、加速度、俯仰角和翻滚角)
 * @param       rawdata：sc7a20h数据结构体
 * @retval      无
 */
void sc7a20h_read_rawdata(sc7a20h_rawdata_t *rawdata)
{
    float sensor_acc_x;
    float sensor_acc_y;
    float sensor_acc_z;

    if (sc7a20h_register_read(SC7A20H_REG_OUT_X_L, xyz_data, 6) != ESP_OK)
    {
        return;
    }

    /* 组合高低字节，SC7A20H为12位数据，左对齐 */
    raw_data[0] = (int16_t)(((uint16_t)xyz_data[1] << 8) | xyz_data[0]) >> 4;
    raw_data[1] = (int16_t)(((uint16_t)xyz_data[3] << 8) | xyz_data[2]) >> 4;
    raw_data[2] = (int16_t)(((uint16_t)xyz_data[5] << 8) | xyz_data[4]) >> 4;

    sensor_acc_x = (float)raw_data[0] * M_G / 1024.0f;
    sensor_acc_y = (float)raw_data[1] * M_G / 1024.0f;
    sensor_acc_z = (float)raw_data[2] * M_G / 1024.0f;

    rawdata->acc_x = sensor_acc_y;
    rawdata->acc_y = -sensor_acc_x;
    rawdata->acc_z = -sensor_acc_z;

    rawdata->acc_g = sqrt(rawdata->acc_x*rawdata->acc_x + rawdata->acc_y * rawdata->acc_y + rawdata->acc_z*rawdata->acc_z);

    acc_normal = sqrtf(rawdata->acc_x * rawdata->acc_x + rawdata->acc_y * rawdata->acc_y + rawdata->acc_z * rawdata->acc_z);
    if (acc_normal == 0.0f)
    {
        rawdata->pitch = 0.0f;
        rawdata->roll = 0.0f;
        return;
    }

    accl_data[0] = rawdata->acc_x / acc_normal;
    accl_data[1] = rawdata->acc_y / acc_normal;
    accl_data[2] = rawdata->acc_z / acc_normal;

    rawdata->pitch = atan2f(rawdata->acc_y, rawdata->acc_z) * RAD_TO_DEG;
    rawdata->roll = atan2f(rawdata->acc_x, rawdata->acc_z) * RAD_TO_DEG;
}

/**
 * @brief       配置自由落体检测
 * @param       threshold：阈值 (mg)
 * @param       duration：持续时间 (ODR周期数)
 * @retval      无
 */
void sc7a20h_config_freefall(uint8_t threshold, uint8_t duration)
{
    /* 配置自由落体阈值 (THS = threshold / 7.81mg) */
    uint8_t ths_value = (uint8_t)(threshold / 7.81f);
    sc7a20h_register_write_byte(0x32, ths_value); /* AOI1_THS */

    /* 配置自由落体持续时间 */
    sc7a20h_register_write_byte(0x33, duration); /* AOI1_DURATION */

    /* 配置中断源为自由落体 */
    sc7a20h_register_write_byte(0x30, 0x90); /* AOI1_CFG (Z低和Y低检测) */
}

/**
 * @brief       初始化sc7a20h
 * @param       无
 * @retval      0, 成功;
                1, 失败;
*/
uint8_t sc7a20h_config(void)
{
    uint8_t id_data = 0;

    /* 读取设备ID */
    sc7a20h_register_read(SC7A20H_REG_WHO_AM_I, &id_data, 1);

    /* 检查设备ID */
    if (id_data != SC7A20H_ID) 
    {
        ESP_LOGE("sc7a20h", "Device ID mismatch: expected 0x%02X, got 0x%02X", SC7A20H_ID, id_data);
        return 1;
    }

    /* 配置控制寄存器1: 100Hz ODR, 使能三轴, 正常模式 */
    uint8_t ctrl_reg1_val = SC7A20H_ODR_100HZ | SC7A20H_ENABLE_ALL_AXES;
    sc7a20h_register_write_byte(SC7A20H_REG_CTRL1, ctrl_reg1_val);

    /* 配置控制寄存器4: ±2G量程, 块数据更新使能 */
    uint8_t ctrl_reg4_val = SC7A20H_SCALE_2G | SC7A20H_BDU_ENABLE;
    sc7a20h_register_write_byte(SC7A20H_REG_CTRL4, ctrl_reg4_val);

    /* 配置为正常模式 */
    sc7a20h_register_write_byte(0x2E, 0x00); /* FIFO_CTRL_REG (禁用FIFO) */
    sc7a20h_register_write_byte(0x24, 0x00); /* CTRL_REG5 (禁用高通滤波器) */

    /* 设置量程对应的缩放因子 */
    scale_factor = 0.001f; /* ±2G量程，1mg/digit */

    ESP_LOGI("sc7a20h", "SC7A20H initialized successfully!");
    return 0;
}

/**
 * @brief       sc7a20h初始化
 * @param       无
 * @retval      无
 */
esp_err_t sc7a20h_init(void)
{
    /* 未调用myiic_init初始化IIC */
    if (bus_handle == NULL)
    {
        ESP_ERROR_CHECK(myiic_init());
    }

    i2c_device_config_t sc7a20h_i2c_dev_conf = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,  /* 从机地址长度 */
        .scl_speed_hz    = IIC_SPEED_CLK,       /* 传输速率 */
        .device_address  = SC7A20H_ADDR,        /* 从机7位的地址 */
    };
    /* I2C总线上添加sc7a20h设备 */
    ESP_ERROR_CHECK(i2c_master_bus_add_device(bus_handle, &sc7a20h_i2c_dev_conf, &sc7a20h_handle));

    while (sc7a20h_config())   /* 检测不到sc7a20h */
    {
        ESP_LOGE("sc7a20h", "sc7a20h init fail!!!");
        vTaskDelay(500);
    }
    while(1)
    {
        delay_ms(1000);
        c7a20h_read_rawdata(&xyz_rawdata);
        ESP_LOGE("acc_x=%f,acc_y=%f,acc_z=%f,pitch=%f,roll=%f",xyz_rawdata.acc_x,xyz_rawdata.acc_y,xyz_rawdata.acc_z,xyz_rawdata.pitch,xyz_rawdata.roll);
    }
    return 0;
}