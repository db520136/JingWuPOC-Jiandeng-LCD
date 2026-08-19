//////////////////////////////////////////////////////////////////////////////////	 
//本程序只供学习使用，未经作者许可，不得用于其它商业用途
//测试硬件：单片机STM32F103C8T6,F103C8T6核心开发板,主频72MHZ，晶振8MHZ
//QDtech-TFT液晶驱动 for STM32 IO模拟
//Chan@ShenZhen QDtech co.,LTD
//公司网站:www.qdtft.com
//wiki技术资料网站：http://www.lcdwiki.com
//我司提供技术支持，任何技术问题欢迎随时交流学习
//固话(传真) :+86 0755-21077707 
//手机: (销售)18823372746 （技术)15989313508
//邮箱:(销售/订单) sales@qdtft.com  (售后/技术服务)service@qdtft.com
//QQ:(售前咨询)3002706772 (技术支持)3002778157
//技术交流QQ群:778679828
//创建日期:2020/05/07
//版本：V1.0
//版权所有，盗版必究。
//Copyright(C) 深圳市全动电子技术有限公司 2018-2028
//All rights reserved
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
 /* @attention
  *
  * THE PRESENT FIRMWARE WHICH IS FOR GUIDANCE ONLY AIMS AT PROVIDING CUSTOMERS
  * WITH CODING INFORMATION REGARDING THEIR PRODUCTS IN ORDER FOR THEM TO SAVE
  * TIME. AS A RESULT, QD electronic SHALL NOT BE HELD LIABLE FOR ANY
  * DIRECT, INDIRECT OR CONSEQUENTIAL DAMAGES WITH RESPECT TO ANY CLAIMS ARISING
  * FROM THE CONTENT OF SUCH FIRMWARE AND/OR THE USE MADE BY CUSTOMERS OF THE
  * CODING INFORMATION CONTAINED HEREIN IN CONNECTION WITH THEIR PRODUCTS.
**************************************************************************************************/	
#include "flash.h"
#include "delay.h"
 
#if FLASH_WREN	//如果使能了写   

/*****************************************************************************
 * @name       :void FLASH_Write_NoCheck(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite) 
 * @date       :2020-03-02 
 * @function   :unchecked to write to flash
 * @parameters :WriteAddr:the start address to be written of flash 
								pBuffer:the point of written data
								NumToWrite:the number of halfword to be written
 * @retvalue   :None
******************************************************************************/   
void FLASH_Write_NoCheck(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite)   
{ 			 		 
	u16 i;
	for(i=0;i<NumToWrite;i++)
	{
		FLASH_ProgramHalfWord(WriteAddr,pBuffer[i]);
	    WriteAddr+=2;//地址增加2.
	}  
} 

#if FLASH_SIZE<256
#define FLASH_SECTOR_SIZE 1024 //字节
#else 
#define FLASH_SECTOR_SIZE	2048
#endif		 
u16 FLASH_BUF[FLASH_SECTOR_SIZE/2];//最多是2K字节

/*****************************************************************************
 * @name       :void FLASH_Write_NoCheck(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite) 
 * @date       :2020-03-02 
 * @function   :Write data of the specified length from the specified address
 * @parameters :WriteAddr:the start address to be written of flash 
								pBuffer:the point of written data
								NumToWrite:the number of halfword to be written
 * @retvalue   :None
******************************************************************************/  
void FLASH_Write(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite)	
{
	u32 secpos;	   //扇区地址
	u16 secoff;	   //扇区内偏移地址(16位字计算)
	u16 secremain; //扇区内剩余地址(16位字计算)	   
 	u16 i;    
	u32 offaddr;   //去掉0X08000000后的地址
	if(WriteAddr<FLASH_BASE_ADDRESS||(WriteAddr>=(FLASH_BASE_ADDRESS+1024*FLASH_SIZE)))return;//非法地址
	FLASH_Unlock();						//解锁
	offaddr=WriteAddr-FLASH_BASE_ADDRESS;		//实际偏移地址.
	secpos=offaddr/FLASH_SECTOR_SIZE;			//扇区地址  0~127 for STM32F103RBT6
	secoff=(offaddr%FLASH_SECTOR_SIZE)/2;		//在扇区内的偏移(2个字节为基本单位.)
	secremain=FLASH_SECTOR_SIZE/2-secoff;		//扇区剩余空间大小   
	if(NumToWrite<=secremain)secremain=NumToWrite;//不大于该扇区范围
	while(1) 
	{	
		FLASH_Read(secpos*FLASH_SECTOR_SIZE+FLASH_BASE_ADDRESS,FLASH_BUF,FLASH_SECTOR_SIZE/2);//读出整个扇区的内容
		for(i=0;i<secremain;i++)//校验数据
		{
			if(FLASH_BUF[secoff+i]!=0XFFFF)break;//需要擦除  	  
		}
		if(i<secremain)//需要擦除
		{
			FLASH_ErasePage(secpos*FLASH_SECTOR_SIZE+FLASH_BASE_ADDRESS);//擦除这个扇区
			for(i=0;i<secremain;i++)//复制
			{
				FLASH_BUF[i+secoff]=pBuffer[i];	  
			}
			FLASH_Write_NoCheck(secpos*FLASH_SECTOR_SIZE+FLASH_BASE_ADDRESS,FLASH_BUF,FLASH_SECTOR_SIZE/2);//写入整个扇区  
		}else FLASH_Write_NoCheck(WriteAddr,pBuffer,secremain);//写已经擦除了的,直接写入扇区剩余区间. 				   
		if(NumToWrite==secremain)break;//写入结束了
		else//写入未结束
		{
			secpos++;				//扇区地址增1
			secoff=0;				//偏移位置为0 	 
		   	pBuffer+=secremain;  	//指针偏移
			WriteAddr+=secremain;	//写地址偏移	   
		   	NumToWrite-=secremain;	//字节(16位)数递减
			if(NumToWrite>(FLASH_SECTOR_SIZE/2))secremain=FLASH_SECTOR_SIZE/2;//下一个扇区还是写不完
			else secremain=NumToWrite;//下一个扇区可以写完了
		}	 
	};	
	FLASH_Lock();//上锁
}
#endif

/*****************************************************************************
 * @name       :void FLASH_Write_NoCheck(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite) 
 * @date       :2020-03-02 
 * @function   :Read data of the specified length from the specified address
 * @parameters :WriteAddr:the start address to be read of flash 
								pBuffer:the point of read data
								NumToWrite:the number of halfword to be read
 * @retvalue   :None
******************************************************************************/ 
void FLASH_Read(u32 ReadAddr,u16 *pBuffer,u16 NumToRead)   	
{
	u16 i;
	for(i=0;i<NumToRead;i++)
	{
		pBuffer[i]=*(vu16*)ReadAddr;//读取2个字节.
		ReadAddr+=2;//偏移2个字节.	
	}
}

















