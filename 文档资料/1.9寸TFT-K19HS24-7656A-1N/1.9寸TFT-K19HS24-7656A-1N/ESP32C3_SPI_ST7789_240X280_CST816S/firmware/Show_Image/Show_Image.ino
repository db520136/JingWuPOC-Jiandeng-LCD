#include "Wire.h"
#include <Arduino_GFX_Library.h>
#include "image.h"

#define GFX_BL 10
Arduino_DataBus *bus = new Arduino_ESP32SPI(6 /* DC */, 4 /* CS */, 7 /* SCK */, 5 /* MOSI */);
Arduino_GFX *gfx = new Arduino_ST7789(bus, -1 /* RST */, 0/* rotation */, true/*IPS*/, 240/* width */, 296/* height */, 0/* col offset 1 */, 0/* row offset 1 */, 0, 0);

#define TouchI2CAddr 0x15


void setup(void)
{
  /*显示初始化*/
  gfx->begin();   //初始化LCD
  gfx->fillScreen(BLACK);   //背景色黑色
  /*背光设置*/
  pinMode(GFX_BL, OUTPUT);
  digitalWrite(GFX_BL, HIGH);
  gfx->draw16bitRGBBitmap(0,0,(const uint16_t*)Image_240x240,240,240);   //显示一张图片(LOGO)

  /*触摸初始化*/
  Wire.begin();

  delay(5000); // 5 seconds
  //gfx->fillScreen(RED);   //背景色红色
}

void loop()
{
  Wire.beginTransmission(TouchI2CAddr);   //开始传输
  Wire.requestFrom(TouchI2CAddr,1);
  Wire.endTransmission();
  
  delay(1000); // 1 second
}
