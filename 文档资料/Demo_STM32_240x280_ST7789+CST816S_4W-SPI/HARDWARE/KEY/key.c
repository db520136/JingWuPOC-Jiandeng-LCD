/****************************************************************************************************
//=========================================电源接线================================================//
//     LCD模块                STM32单片机
//      VCC          接        DC5V/3.3V      //电源
//      GND          接          GND          //电源地
//=======================================液晶屏数据线接线==========================================//
//本模块默认数据总线类型为SPI总线
//     LCD模块                STM32单片机    
//    SDI(MOSI)      接          PA7         //液晶屏SPI总线数据写信号
//    SDO(MISO)      接          PA6         //液晶屏SPI总线数据读信号，如果不需要读，可以不接线
//=======================================液晶屏控制线接线==========================================//
//     LCD模块 					      STM32单片机 
//       LED         接          PB6         //液晶屏背光控制信号，如果不需要控制，接5V或3.3V
//       SCK         接          PA5         //液晶屏SPI总线时钟信号
//      DC/RS        接          PB7         //液晶屏数据/命令控制信号
//       RST         接          PB8         //液晶屏复位控制信号
//       CS          接          PB9         //液晶屏片选控制信号
//=========================================触摸屏触接线=========================================//
//如果模块不带触摸功能或者带有触摸功能，但是不需要触摸功能，则不需要进行触摸屏接线
//	   LCD模块                STM32单片机 
//      T_IRQ        接          PA1         //触摸屏触摸中断信号
//      T_DO         接          PA8         //触摸屏SPI总线读信号
//      T_DIN        接          PA9         //触摸屏SPI总线写信号
//      T_CS         接          PA10        //触摸屏片选控制信号
//      T_CLK        接          PB5         //触摸屏SPI总线时钟信号
**************************************************************************************************/			
#include "key.h"
#include "delay.h"


/*****************************************************************************
 * @name       :void KEY_Init(void)
 * @date       :2018-08-09 
 * @function   :Initialization key GPIO
 * @parameters :None
 * @retvalue   :None
******************************************************************************/ 
void KEY_Init(void)
{
	
	GPIO_InitTypeDef GPIO_InitStructure;

 	RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA,ENABLE);//使能PORTA,PORTC时钟
	GPIO_InitStructure.GPIO_Pin  = GPIO_Pin_0;//PA15
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IPD; //设置成上拉输入
 	GPIO_Init(GPIOA, &GPIO_InitStructure);//初始化GPIOA15

} 

/*****************************************************************************
 * @name       :u8 KEY_Scan(void)
 * @date       :2018-08-09 
 * @function   :Key processing,Response priority:KEY0>KEY1>WK_UP!!
 * @parameters :None
 * @retvalue   :0-No buttons were pressed.
								1-The KEY0 button is pressed
								2-The KEY1 button is pressed
								3-The WK_UP button is pressed
******************************************************************************/ 
u8 KEY_Scan(void)
{	 	  
	if(KEY0==1)
	{
		delay_ms(10);//去抖动 
		if(KEY0==1)return KEY0_PRES;
	}	     
	return 0;// 无按键按下
}
