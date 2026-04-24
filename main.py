# -*- coding:utf-8 -*-
import asyncio
import json
import math
import os
import re
import time
import logging
import traceback
import hashlib
import random
from base64 import b64decode

try:
    from fake_useragent import UserAgent
except ImportError:
    class UserAgent:  # type: ignore[override]
        @property
        def random(self):
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

import aiofiles
import aiohttp
import requests
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import selenium.common.exceptions
import colorama

from conf import BASE_DIR, RESULT_PATH
from utils.exceptions import XMLimitError

colorama.init(autoreset=True)
logger = logging.getLogger('logger')
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler('app.log', mode='w', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
ua = UserAgent()



class Ximalaya:
    def __init__(self, account_name="vip"):
        self.default_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "dnt": "1",
            "origin": "https://www.ximalaya.com",
            "priority": "u=1, i",
            "sec-ch-ua": '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        self.conf_path = BASE_DIR / "config" / f"{account_name}.conf"
        self.download_path = RESULT_PATH / ".tmp" / "ximalaya"

    def build_browser_options(self):
        option = webdriver.ChromeOptions()
        option.add_experimental_option("detach", False)
        option.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        option.add_experimental_option('useAutomationExtension', False)
        option.add_argument("--disable-blink-features=AutomationControlled")
        return option

    def create_stealth_driver(self):
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=self.build_browser_options(),
        )
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })
        return driver

    # 生成喜马拉雅新版签名 xm-sign (V2)
    def get_xm_sign(self, cookie_str=""):
        try:
            # 现代签名规则：BrowserID&&SessionID
            # BrowserID 对应 cookie 中的 wfp
            # SessionID 对应 cookie 中的 HWWAFSESID
            cookies = {}
            if cookie_str:
                for c in cookie_str.split(';'):
                    if '=' in c:
                        k, v = c.strip().split('=', 1)
                        cookies[k] = v
            
            wfp = cookies.get('wfp', 'ACM3OGU0YjlkYzZlZjAzM2Rlz_rWpwVpJOV4bXdlYl93d3c') # 默认值可能无效，建议使用真实cookie
            sessid = cookies.get('HWWAFSESID', '')
            
            # 如果没有 sessid，可能是未初始化会话
            # xm-sign 格式: wfp&&sessid
            sign = f"{wfp}&&{sessid}"
            return sign, wfp
        except Exception as e:
            logger.error(f"生成签名失败: {e}")
            return "&&", ""

    def get_headers(self, cookie=None):
        headers = self.default_headers.copy()
        if not cookie:
            cookie = self.analyze_config().replace('"', '') # 去除多余引号

        if cookie:
            headers["cookie"] = cookie
        sign, wfp = self.get_xm_sign(cookie)
        if sign != "&&":
            headers["xm-sign"] = sign
        if wfp:
            headers["xm-fp"] = wfp
        headers["xm-page-viewid"] = f"{int(time.time()*1000)}{random.randint(100, 999)}"
        return headers

    def parse_browser_album_data(self, album_name, track_links):
        sounds = []
        seen_track_ids = set()
        clean_album_name = album_name.strip()

        for index, track in enumerate(track_links, start=1):
            href = (track.get("href") or "").strip()
            title = (track.get("text") or "").strip()
            match = re.search(r"/sound/(\d+)", href)
            if not match:
                continue

            track_id = int(match.group(1))
            if track_id in seen_track_ids:
                continue
            seen_track_ids.add(track_id)

            sounds.append({
                "trackId": track_id,
                "albumTitle": clean_album_name,
                "title": title,
                "index": len(sounds) + 1,
            })

        return clean_album_name, sounds

    def parse_mobile_album_data(self, tracks):
        if not tracks:
            return False, False

        album_name = (tracks[0].get("albumTitle") or "").strip()
        sounds = []
        for index, track in enumerate(tracks, start=1):
            if not track.get("trackId"):
                continue
            sound = dict(track)
            sound["albumTitle"] = (sound.get("albumTitle") or album_name).strip()
            sound["title"] = (sound.get("title") or "").strip()
            sound["index"] = index
            sounds.append(sound)
        if not sounds:
            return False, False
        return album_name, sounds

    def build_track_api_error(self, sound_id, response_json):
        ret = response_json.get("ret")
        msg = response_json.get("msg", "")
        if ret == 1001:
            return {
                "__xm_error__": "system_busy",
                "sound_id": sound_id,
                "ret": ret,
                "msg": msg,
            }
        return {
            "__xm_error__": "unexpected_response",
            "sound_id": sound_id,
            "ret": ret,
            "msg": msg,
        }

    def parse_track_api_response(self, sound_id, response_json):
        track_info = response_json.get("trackInfo")
        if track_info:
            return track_info
        return self.build_track_api_error(sound_id, response_json)

    def scrape_album_from_mobile_api(self, album_id):
        logger.debug(f'开始使用移动端接口解析专辑 {album_id}')
        url = "https://mobile.ximalaya.com/mobile/playlist/album/page"
        all_tracks = []
        try:
            response = requests.get(url, params={"albumId": album_id, "pageId": 1}, timeout=15)
            res_json = response.json()
            if res_json.get("ret") != 0:
                logger.debug(f"移动端接口第一页失败: {res_json}")
                return False, False

            all_tracks.extend(res_json.get("list") or [])
            max_page_id = int(res_json.get("maxPageId") or 1)
            for page_id in range(2, max_page_id + 1):
                page_response = requests.get(
                    url,
                    params={"albumId": album_id, "pageId": page_id},
                    timeout=15,
                )
                page_json = page_response.json()
                if page_json.get("ret") != 0:
                    logger.debug(f"移动端接口第{page_id}页失败: {page_json}")
                    return False, False
                all_tracks.extend(page_json.get("list") or [])

            album_name, sounds = self.parse_mobile_album_data(all_tracks)
            if sounds:
                logger.debug(f'移动端接口解析专辑成功，获取到 {len(sounds)} 个声音')
            return album_name, sounds
        except Exception:
            logger.debug(f'移动端接口解析专辑 {album_id} 失败')
            logger.debug(traceback.format_exc())
            return False, False

    def scrape_album_from_browser(self, album_id):
        logger.debug(f'API命中风控，开始使用浏览器解析专辑 {album_id}')
        driver = None
        try:
            driver = self.create_stealth_driver()
            album_url = f"https://www.ximalaya.com/album/{album_id}"
            driver.get(album_url)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script(
                    "return document.querySelectorAll('a[href*=\"/sound/\"]').length"
                ) > 0
            )

            stable_rounds = 0
            previous_count = 0
            for _ in range(15):
                time.sleep(0.8)
                current_count = driver.execute_script(
                    "return document.querySelectorAll('a[href*=\"/sound/\"]').length"
                )
                if current_count == previous_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    previous_count = current_count
                if stable_rounds >= 2:
                    break
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            browser_data = driver.execute_script("""
                const titleNode = document.querySelector('h1');
                const pageTitle = document.title || '';
                const albumName = (titleNode && titleNode.textContent.trim())
                    || pageTitle.split('_')[0].trim()
                    || pageTitle.trim();
                const trackLinks = Array.from(document.querySelectorAll('a[href*="/sound/"]'))
                    .map((node) => ({
                        text: (node.textContent || '').trim(),
                        href: node.href || '',
                    }));
                return { albumName, trackLinks };
            """)
            album_name, sounds = self.parse_browser_album_data(
                browser_data.get("albumName", ""),
                browser_data.get("trackLinks", []),
            )
            if not sounds:
                return False, False
            logger.debug(f'浏览器解析专辑成功，获取到 {len(sounds)} 个声音')
            return album_name, sounds
        except Exception:
            logger.debug(f'浏览器解析专辑 {album_id} 失败')
            logger.debug(traceback.format_exc())
            return False, False
        finally:
            if driver:
                driver.quit()

    # 解析声音，如果成功返回声音名和声音链接，否则返回False
    def analyze_sound(self, sound_id, headers):
        logger.debug(f'开始解析ID为{sound_id}的声音')
        url = f"https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/{int(time.time() * 1000)}"
        params = {
            "device": "www2",
            "trackId": sound_id,
            "trackQualityLevel": 2
        }
        request_headers = dict(headers)
        request_headers["referer"] = f"https://www.ximalaya.com/sound/{sound_id}"
        try:
            response = requests.get(url, headers=request_headers, params=params, timeout=15)
        except Exception as e:
            print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败！')
            logger.debug(f'ID为{sound_id}的声音解析失败！')
            logger.debug(traceback.format_exc())
            return False
        response_json = response.json()
        track_info = self.parse_track_api_response(sound_id, response_json)
        if isinstance(track_info, dict) and track_info.get("__xm_error__"):
            if track_info["__xm_error__"] == "system_busy":
                print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败，接口返回系统繁忙，疑似被风控或限流')
            else:
                print(
                    colorama.Fore.RED
                    + f"ID为{sound_id}的声音解析失败，接口返回异常: ret={track_info.get('ret')} msg={track_info.get('msg')}"
                )
            logger.debug(f'ID为{sound_id}的声音接口异常: {response_json}')
            return False
        try:
            not track_info["isAuthorized"]
        except KeyError:
            print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败，接口返回数据缺失！')
            return False
        if not track_info["isAuthorized"]:
            return 0  # 未购买或未登录vip账号
        try:
            sound_name = track_info["title"]
            encrypted_url_list = track_info["playUrlList"]
        except Exception as e:
            print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败！')
            logger.debug(f'ID为{sound_id}的声音解析失败！')
            logger.debug(traceback.format_exc())
            return False
        if encrypted_url_list[0]["type"][:2] == "AI":
            sound_info = {"name": sound_name, 0: "", 1: "", 2: ""}
            sound_info[0] = sound_info[1] = self.decrypt_url(encrypted_url_list[0]["url"])
            logger.debug(f'ID为{sound_id}的声音解析成功！')
            return sound_info
        else:
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

    # 解析专辑，如果成功返回专辑名和专辑声音列表，否则返回False
    def analyze_album(self, album_id):
        logger.debug(f'开始解析ID为{album_id}的专辑')
        url = "https://www.ximalaya.com/revision/album/v1/getTracksList"
        params = {
            "albumId": album_id,
            "pageNum": 1,
            "sort": 0,
            "pageSize": 30
        }
        headers = self.get_headers()
        headers["authority"] = "www.ximalaya.com"
        headers["referer"] = f"https://www.ximalaya.com/album/{album_id}"
        retries = 5
        while True:
            try:
                response = requests.get(url, headers=headers, params=params, timeout=15)
                res_json = response.json()
            except Exception as e:
                print(colorama.Fore.RED + f'ID为{album_id}的专辑解析失败！')
                logger.debug(f'ID为{album_id}的专辑解析失败！')
                logger.debug(traceback.format_exc())
                raise XMLimitError(
                    f"xm analyze_album error(unknown reason): {e}")
            tracks = res_json.get("data", {}).get("tracks") or []
            if tracks and res_json.get("ret") in (0, 200):
                break
            if res_json.get("data", {}).get("riskLevel"):
                logger.debug(f"专辑接口命中风控: {res_json}")
                mobile_result = self.scrape_album_from_mobile_api(album_id)
                if mobile_result != (False, False):
                    return mobile_result
                return self.scrape_album_from_browser(album_id)
            if tracks == []:
                logger.debug(f"服务端返回数据为空，完整内容: {res_json}")
                retries -= 1
            elif res_json.get("ret") not in (0, 200):
                logger.debug(f"接口返回码错误: {res_json}")
                retries -= 1
            if retries == 0:
                logger.debug(f'ID为{album_id}的专辑接口多次失败，切换浏览器兜底')
                mobile_result = self.scrape_album_from_mobile_api(album_id)
                if mobile_result != (False, False):
                    return mobile_result
                return self.scrape_album_from_browser(album_id)
        pages = math.ceil(response.json()["data"]["trackTotalCount"] / params["pageSize"])
        sounds = []
        for page in range(1, pages + 1):
            params = {
                "albumId": album_id,
                "pageNum": page,
                "sort": 0,
                "pageSize": 30
            }
            retries = 5
            while True:
                try:
                    headers = self.get_headers() # 重新生成包含签名的头部
                    headers["referer"] = f"https://www.ximalaya.com/album/{album_id}"
                    response = requests.get(url, headers=headers, params=params, timeout=30)
                    res_json = response.json()
                except Exception as e:
                    print(colorama.Fore.RED + f'ID为{album_id}的专辑解析失败！')
                    logger.debug(f'ID为{album_id}的专辑解析失败！')
                    logger.debug(traceback.format_exc())
                    mobile_result = self.scrape_album_from_mobile_api(album_id)
                    if mobile_result != (False, False):
                        return mobile_result
                    return self.scrape_album_from_browser(album_id)
                page_tracks = res_json.get("data", {}).get("tracks") or []
                if res_json.get("data", {}).get("riskLevel"):
                    logger.debug(f"翻页命中风控，切换浏览器解析: {res_json}")
                    mobile_result = self.scrape_album_from_mobile_api(album_id)
                    if mobile_result != (False, False):
                        return mobile_result
                    return self.scrape_album_from_browser(album_id)
                if page_tracks == []:
                    print(f"第{page}页解析失败第{6-retries}次，共{pages}页")
                    retries -= 1
                else:
                    print(f"第{page}页解析成功，共{pages}页")
                    break
                if retries == 0:
                    logger.debug(f'第{page}页接口重试失败，切换浏览器解析')
                    mobile_result = self.scrape_album_from_mobile_api(album_id)
                    if mobile_result != (False, False):
                        return mobile_result
                    return self.scrape_album_from_browser(album_id)
            sounds += page_tracks
        album_name = sounds[0]["albumTitle"]
        logger.debug(f'ID为{album_id}的专辑解析成功')
        return album_name, sounds

    # 协程解析声音
    async def async_analyze_sound(self, sound_id, session, headers):
        retries = 3
        url = f"https://www.ximalaya.com/mobile-playpage/track/v3/baseInfo/{int(time.time() * 1000)}"
        params = {
            "device": "www2",
            "trackId": sound_id,
            "trackQualityLevel": 2
        }
        request_headers = dict(headers)
        request_headers["referer"] = f"https://www.ximalaya.com/sound/{sound_id}"
        sign, wfp = self.get_xm_sign(request_headers.get("cookie", ""))
        request_headers["xm-sign"] = sign
        if wfp:
            request_headers["xm-fp"] = wfp
        while retries > 0:
            try:
                async with session.get(url, headers=request_headers, params=params, timeout=20) as response:
                    response_json = json.loads(await response.text())
                    track_info = self.parse_track_api_response(sound_id, response_json)
                    if isinstance(track_info, dict) and track_info.get("__xm_error__"):
                        if track_info["__xm_error__"] == "system_busy":
                            print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败，接口返回系统繁忙，疑似被风控或限流')
                        else:
                            print(
                                colorama.Fore.RED
                                + f"ID为{sound_id}的声音解析失败，接口返回异常: ret={track_info.get('ret')} msg={track_info.get('msg')}"
                            )
                        logger.debug(f'ID为{sound_id}的声音接口异常: {response_json}')
                        return track_info
                    sound_name = track_info["title"]
                    encrypted_url_list = track_info["playUrlList"]
                    break
            except KeyError:
                print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败，接口返回数据缺失')
                return False
            except Exception as e:
                logger.debug(f'ID为{sound_id}的声音解析失败！')
                logger.debug(traceback.format_exc())
                if retries == 0:
                    print(colorama.Fore.RED + f'ID为{sound_id}的声音解析失败！')
                    return False
            retries -= 1
        if not track_info["isAuthorized"]:
            return 0  # 未购买或未登录vip账号
        if encrypted_url_list[0]["type"][:2] == "AI":
            sound_info = {"name": sound_name, 0: "", 1: "", 2: ""}
            sound_info[0] = sound_info[1] = self.decrypt_url(encrypted_url_list[0]["url"])
            logger.debug(f'ID为{sound_id}的声音解析成功！')
            return sound_info
        else:
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
                retries -= 1
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
        track_api_errors = [
            result for result in sounds_info
            if isinstance(result, dict) and result.get("__xm_error__")
        ]
        if track_api_errors:
            await session.close()
            first_error = track_api_errors[0]
            if first_error.get("__xm_error__") == "system_busy":
                raise XMLimitError("xm 单集解析接口返回 ret=1001（系统繁忙），这是风控/限流，不是 judge_album 误判")
            raise XMLimitError(
                f"xm 单集解析接口返回异常: ret={first_error.get('ret')} msg={first_error.get('msg')}"
            )
        # xm加密链接全部解密失败，意味着可能账号超出限制，需要手动下载
        if not all(sounds_info):
            await session.close()
            raise XMLimitError("xm 单集解析失败，可能未购买、cookie失效，或接口受限")
        tasks = []
        if number:
            num = start
            for sound_info in sounds_info:
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
    def decrypt_url(self, encrypted_url):
        o = bytes([183, 174, 108, 16, 131, 159, 250, 5, 239, 110, 193, 202, 153, 137, 251, 176, 119, 150, 47, 204, 97, 237, 1, 71, 177, 42, 88, 218, 166, 82, 87, 94, 14, 195, 69, 127, 215, 240, 225, 197, 238, 142, 123, 44, 219, 50, 190, 29, 181, 186, 169, 98, 139, 185, 152, 13, 141, 76, 6, 157, 200, 132, 182, 49, 20, 116, 136, 43, 155, 194, 101, 231, 162, 242, 151, 213, 53, 60, 26, 134, 211, 56, 28, 223, 107, 161, 199, 15, 229, 61, 96, 41, 66, 158, 254, 21, 165, 253, 103, 89, 3, 168, 40, 246, 81, 95, 58, 31, 172, 78, 99, 45, 148, 187, 222, 124, 55, 203, 235, 64, 68, 149, 180, 35, 113, 207, 118, 111, 91, 38, 247, 214, 7, 212, 209, 189, 241, 18, 115, 173, 25, 236, 121, 249, 75, 57, 216, 10, 175, 112, 234, 164, 70, 206, 198, 255, 140, 230, 12, 32, 83, 46, 245, 0, 62, 227, 72, 191, 156, 138, 248, 114, 220, 90, 84, 170, 128, 19, 24, 122, 146, 80, 39, 37, 8, 34, 22, 11, 93, 130, 63, 154, 244, 160, 144, 79, 23, 133, 92, 54, 102, 210, 65, 67, 27, 196, 201, 106, 143, 52, 74, 100, 217, 179, 48, 233, 126, 117, 184, 226, 85, 171, 167, 86, 2, 147, 17, 135, 228, 252, 105, 30, 192, 129, 178, 120, 36, 145, 51, 163, 77, 205, 73, 4, 188, 125, 232, 33, 243, 109, 224, 104, 208, 221, 59, 9])
        a = bytes([204, 53, 135, 197, 39, 73, 58, 160, 79, 24, 12, 83, 180, 250, 101, 60, 206, 30, 10, 227, 36, 95, 161, 16, 135, 150, 235, 116, 242, 116, 165, 171])
        encrypted_url = encrypted_url.replace('_', '/').replace('-', '+')
        padding = '=' * (-len(encrypted_url) % 4)
        encrypted_data = b64decode(encrypted_url + padding)
        if len(encrypted_data) < 16:
            return encrypted_url
        data = encrypted_data[:-16]
        iv = encrypted_data[-16:]
        decrypted_data = bytearray(data)
        for i in range(len(decrypted_data)):
            decrypted_data[i] = o[decrypted_data[i]]
        for i in range(0, len(decrypted_data), 16):
            block = decrypted_data[i:i+16]
            decrypted_data[i:i+16] = bytes(a ^ b for a, b in zip(block, iv))
        for i in range(0, len(decrypted_data), 32):
            block = decrypted_data[i:i+32]
            decrypted_data[i:i+32] = bytes(a ^ b for a, b in zip(block, a))
        return decrypted_data.decode('utf-8')

    # 判断专辑是否为付费专辑，如果是免费专辑返回0，如果是已购买的付费专辑返回1，如果是未购买的付费专辑返回2，如果解析失败返回False
    def judge_album(self, album_id, headers):
        logger.debug(f'开始判断ID为{album_id}的专辑的类型')
        url = "https://www.ximalaya.com/revision/album/v1/simple"
        params = {
            "albumId": album_id
        }
        sign, wfp = self.get_xm_sign(headers.get("cookie", ""))
        headers["xm-sign"] = sign
        if wfp:
            headers["xm-fp"] = wfp
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
            "user-agent": ua.random,
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
        option = self.build_browser_options()
        option.add_experimental_option("detach", True)
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=option,
        )
        # 移除 webdriver 标识
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })
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
    album_id = "56066051"
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
