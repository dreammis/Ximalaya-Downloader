import asyncio
import base64
import binascii
import json
import math
import os
import re
import time
import logging
import traceback

import aiofiles
import aiohttp
import requests
from Crypto.Cipher import AES
from webdriver_manager.chrome import ChromeDriverManager
from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import selenium.common.exceptions
import colorama

from conf import BASE_DIR

colorama.init(autoreset=True)
logger = logging.getLogger('logger')
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler('app.log', mode='w', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

class Ximalaya:
    def __init__(self, account_name="vip"):
        self.default_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36 Edg/111.0.1660.14"
        }
        self.conf_path = BASE_DIR / "config" / f"{account_name}.conf"
        self.download_path = BASE_DIR / ".tmp" / "ximalaya"

    # 解析专辑，如果成功返回专辑名和专辑声音列表，否则返回False
    def analyze_album(self, album_id):
        logger.debug(f'开始解析ID为{album_id}的专辑')
        url = "https://www.ximalaya.com/revision/album/v1/getTracksList"
        params = {
            "albumId": album_id,
            "pageNum": 1,
            "pageSize": 100
        }
        try:
            response = requests.get(url, headers=self.default_headers, params=params, timeout=15)
        except Exception as e:
            print(colorama.Fore.RED + f'ID为{album_id}的专辑解析失败！')
            logger.debug(f'ID为{album_id}的专辑解析失败！')
            logger.debug(traceback.format_exc())
            return False, False
        # 在此处增加对于声音的逻辑判断
        pages = math.ceil(response.json()["data"]["trackTotalCount"] / 100)
        sounds = []
        for page in range(1, pages + 1):
            params = {
                "albumId": album_id,
                "pageNum": page,
                "pageSize": 100
            }
            try:
                response = requests.get(url, headers=self.default_headers, params=params, timeout=30)
            except Exception as e:
                print(colorama.Fore.RED + f'ID为{album_id}的专辑解析失败！')
                logger.debug(f'ID为{album_id}的专辑解析失败！')
                logger.debug(traceback.format_exc())
                return False, False
            sounds += response.json()["data"]["tracks"]
        album_name = sounds[0]["albumTitle"]
        logger.debug(f'ID为{album_id}的专辑解析成功')
        return album_name, sounds

    # 协程解析声音
    async def async_analyze_sound(self, sound_id, session, headers):
        url = f"https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/{int(time.time() * 1000)}"
        params = {
            "device": "web",
            "trackId": sound_id,
            "trackQualityLevel": 2
        }
        try:
            async with session.get(url, headers=headers, params=params, timeout=60) as response:
                response_json = json.loads(await response.text())
                sound_name = response_json["trackInfo"]["title"]
                encrypted_url_list = response_json["trackInfo"]["playUrlList"]
        except Exception as e:
            print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败！')
            logger.debug(f'ID为{sound_id}的声音解析失败！')
            logger.debug(traceback.format_exc())
            return False
        if not response_json["trackInfo"]["isAuthorized"]:
            return 0  # 未购买或未登录vip账号
        sound_info = {"name": sound_name, 0: "", 1: "", 2: ""}
        for encrypted_url in encrypted_url_list:
            if encrypted_url["type"] == "M4A_128":
                sound_info[2] = self.decrypt_url(encrypted_url["url"])
            elif encrypted_url["type"] == "MP3_64":
                sound_info[1] = self.decrypt_url(encrypted_url["url"])
            elif encrypted_url["type"] == "MP3_32":
                sound_info[0] = self.decrypt_url(encrypted_url["url"])
        logger.debug(f'ID为{sound_id}的声音解析成功！')
        return sound_info

    # 将文件名中不能包含的字符替换为空格
    def replace_invalid_chars(self, name):
        invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        for char in invalid_chars:
            if char in name:
                name = name.replace(char, " ")
        return name

    # 下载单个声音
    def get_sound(self, sound_name, sound_url, path):
        retries = 3
        sound_name = self.replace_invalid_chars(sound_name)
        if '?' in sound_url:
            type = sound_url.split('?')[0][-3:]
        else:
            type = sound_url[-3:]
        if os.path.exists(f"{path}/{sound_name}.{type}"):
            print(f'{sound_name}已存在！')
            return
        while retries > 0:
            try:
                logger.debug(f'开始下载声音{sound_name}')
                response = requests.get(sound_url, headers=self.default_headers, timeout=60)
                break
            except Exception as e:
                logger.debug(f'{sound_name}第{4 - retries}次下载失败！')
                logger.debug(traceback.format_exc())
                retries -= 1
        if retries == 0:
            print(colorama.Fore.RED + f'{sound_name}下载失败！')
            logger.debug(f'{sound_name}经过三次重试后下载失败！')
            return False
        sound_file = response.content
        if not os.path.exists(path):
            os.makedirs(path)
        with open(f"{path}/{sound_name}.{type}", mode="wb") as f:
            f.write(sound_file)
        print(f'{sound_name}下载完成！')
        logger.debug(f'{sound_name}下载完成！')

    # 协程下载声音
    async def async_get_sound(self, sound_name, sound_url, album_name, session, path, global_retries, num=None):
        retries = 3
        logger.debug(f'开始下载声音{sound_name}')
        if num is None:
            sound_name = self.replace_invalid_chars(sound_name)
        else:
            sound_name = f"{num}-{sound_name}"
            sound_name = self.replace_invalid_chars(sound_name)
        if '?' in sound_url:
            type = sound_url.split('?')[0][-3:]
        else:
            type = sound_url[-3:]
        album_name = self.replace_invalid_chars(album_name)
        album_path = path / f"{album_name}"
        album_path.mkdir(parents=True, exist_ok=True)
        if (path / f"{album_name}/{sound_name}.{type}").exists():
            print(f'{sound_name}已存在！')
            return None
        while retries > 0:
            try:
                async with session.get(sound_url, headers=self.default_headers, timeout=120) as response:
                    async with aiofiles.open(f"{path}/{album_name}/{sound_name}.{type}", mode="wb") as f:
                        await f.write(await response.content.read())
                print(f'{sound_name}下载完成！')
                logger.debug(f'{sound_name}下载完成！')
                break
            except Exception as e:
                logger.debug(f'{sound_name}第{global_retries * 3 + 4 - retries}次下载失败！')
                logger.debug(traceback.format_exc())
                retries += 1
                if os.path.exists(f"{path}/{album_name}/{sound_name}.{type}"):
                    os.remove(f"{path}/{album_name}/{sound_name}.{type}")
        if retries == 0:
            return ([sound_name, sound_url, album_name, session, path, global_retries, num])

    # 下载专辑中的选定声音
    async def get_selected_sounds(self, sounds, album_name, start, end, headers, quality, number, path):
        tasks = []
        global_retries = 0
        max_global_retries = 2
        session = aiohttp.ClientSession()
        digits = len(str(len(sounds)))
        for i in range(start - 1, end):
            sound_id = sounds[i]["trackId"]
            tasks.append(asyncio.create_task(self.async_analyze_sound(sound_id, session, headers)))
        sounds_info = await asyncio.gather(*tasks)
        tasks = []
        if number:
            num = start
            # todo 临时测试设置10
            for sound_info in sounds_info[:5]:
                if sound_info is False or sound_info == 0:
                    continue
                num_ = str(num).zfill(digits)
                if quality == 2 and sound_info[2] == "":
                     quality = 1
                tasks.append(asyncio.create_task(self.async_get_sound(sound_info["name"], sound_info[quality], album_name, session, path, global_retries, num_)))
                num += 1
        else:
            for sound_info in sounds_info:
                if sound_info is False or sound_info == 0:
                    continue
                if quality == 2 and sound_info[2] == "":
                    quality = 1
                tasks.append(asyncio.create_task(self.async_get_sound(sound_info["name"], sound_info[quality], album_name, session, path, global_retries)))
        failed_downloads = [result for result in await asyncio.gather(*tasks) if result is not None]
        while failed_downloads and global_retries < max_global_retries:
            tasks = [asyncio.create_task(self.async_get_sound(*failed_download)) for failed_download in failed_downloads]
            failed_downloads = [result for result in await asyncio.gather(*tasks) if result is not None]
            global_retries += 1
        print("专辑全部选定声音下载完成！")
        if failed_downloads:
            for failed_download in failed_downloads:
                print(colorama.Fore.RED + f'声音{failed_download[0]}下载失败！')
        await session.close()

    # 解密vip声音url
    def decrypt_url(self, ciphertext):
        key = binascii.unhexlify("aaad3e4fd540b0f79dca95606e72bf93")
        ciphertext = base64.urlsafe_b64decode(ciphertext + '=' * (4 - len(ciphertext) % 4))
        cipher = AES.new(key, AES.MODE_ECB)
        plaintext = cipher.decrypt(ciphertext)
        plaintext = re.sub(r"[^\x20-\x7E]", "", plaintext.decode("utf-8"))
        return plaintext

    # 判断专辑是否为付费专辑，如果是免费专辑返回0，如果是已购买的付费专辑返回1，如果是未购买的付费专辑返回2，如果解析失败返回False
    def judge_album(self, album_id, headers):
        logger.debug(f'开始判断ID为{album_id}的专辑的类型')
        url = "https://www.ximalaya.com/revision/album/v1/simple"
        params = {
            "albumId": album_id
        }
        try:
            response = requests.get(url, headers=headers, params=params, timeout=15)
        except Exception as e:
            print(colorama.Fore.RED + f'ID为{album_id}的专辑解析失败！')
            logger.debug(f'ID为{album_id}的专辑判断类型失败！')
            logger.debug(traceback.format_exc())
            return False
        logger.debug(f'ID为{album_id}的专辑判断类型成功！')
        if not response.json()["data"]["albumPageMainInfo"]["isPaid"]:
            return 0  # 免费专辑
        elif response.json()["data"]["albumPageMainInfo"]["hasBuy"]:
            return 1  # 已购专辑
        else:
            return 2  # 未购专辑

    # 获取配置文件中的cookie和path
    def analyze_config(self) -> str:
        try:
            with open(self.conf_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return ""

    # 判断cookie是否有效
    def judge_cookie(self, cookie):
        url = "https://www.ximalaya.com/revision/my/getCurrentUserInfo"
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36 Edg/111.0.1660.14",
            "cookie": cookie
        }
        try:
            response = requests.get(url, headers=headers, timeout=15)
        except Exception as e:
            print("无法获取喜马拉雅用户数据，请检查网络状况！")
            logger.debug("无法获取喜马拉雅用户数据！")
            logger.debug(traceback.format_exc())
        if response.json()["ret"] == 200:
            return response.json()["data"]["userName"]
        else:
            return False

    # 登录喜马拉雅账号
    def login(self):
        print("在浏览器中登录并自动提取cookie")
        print("Google Chrome")
        option = webdriver.ChromeOptions()
        option.add_experimental_option("detach", True)
        option.add_experimental_option('excludeSwitches', ['enable-logging'])
        driver = webdriver.Chrome(ChromeDriverManager().install(), options=option)
        print("请在弹出的浏览器中登录喜马拉雅账号，登陆成功浏览器会自动关闭")
        driver.get("https://passport.ximalaya.com/page/web/login")
        try:
            WebDriverWait(driver, 300).until(EC.url_to_be("https://www.ximalaya.com/"))
            cookies = driver.get_cookies()
            logger.debug('以下是使用浏览器登录喜马拉雅账号时的浏览器日志：')
            for entry in driver.get_log('browser'):
                logger.debug(entry['message'])
            logger.debug('浏览器日志结束')
            driver.quit()
        except selenium.common.exceptions.TimeoutException:
            print("登录超时，自动返回主菜单！")
            logger.debug('以下是使用浏览器登录喜马拉雅账号时的浏览器日志：')
            for entry in driver.get_log('browser'):
                logger.debug(entry['message'])
            logger.debug('浏览器日志结束')
            driver.quit()
            return
        cookie = ""
        for cookie_ in cookies:
            cookie += f"{cookie_['name']}={cookie_['value']}; "
        with open(self.conf_path, "w", encoding="utf-8") as f:
            json.dump(cookie, f)
        username = self.judge_cookie(cookie)
        print(f"成功登录账号{username}！")
        return cookie



if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    album_id = "77789950"
    ximalaya = Ximalaya()
    cookie = ximalaya.analyze_config()

    if not cookie or not ximalaya.judge_cookie(ximalaya.analyze_config()):
        print("登录信息过期重新登录！")
        if not ximalaya.login():
            print("似乎登录了也没卵用")
            exit()

    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36 Edg/111.0.1660.14",
        "cookie": cookie
    }

    album_name, sounds = ximalaya.analyze_album(album_id)
    if not sounds:
        exit()
    album_type = ximalaya.judge_album(album_id, headers)
    if album_type == 0:
        print(f"成功解析免费专辑{album_id}，专辑名{album_name}，共{len(sounds)}个声音")
    elif album_type == 1:
        print(f"成功解析已购付费专辑{album_id}，专辑名{album_name}，共{len(sounds)}个声音")
    else:
        print(f"成功解析付费专辑{album_id}，专辑名{album_name}，但是当前登陆账号未购买此专辑或未开通vip")
    start = 1
    end = len(sounds)
    quality = 0 # 0 低质量 1 普通 2 高质量
    loop.run_until_complete(ximalaya.get_selected_sounds(sounds, album_name, start, end, headers, 0, True, ximalaya.download_path))
