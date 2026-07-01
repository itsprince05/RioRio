import requests as req
import base64
import re
import os
import subprocess
import hashlib
import asyncio
import aiohttp
import time
import logging
from Crypto.Cipher import AES
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from config import Config

logger=logging.getLogger(__name__)

class PFMDownloader:

    def __init__(self):
        self.base = Config.PFM_API_BASE

        self.header={
            "user-agent": Config.PFM_USER_AGENT,
            "platform": Config.PFM_PLATFORM,
            "app-version": Config.PFM_APP_VERSION
        }
        
        self.token = {}
        self.refresh_auth_token()

        self.endpoint = [
            '/v2/feed_api/get_saved_stories',
            '/v2/user_api/user_action.update',
            '/v2/content_api/show.get_episodes',
            '/v2/content_api/show.play_details',
            '/v2/content_api/show.get_details'
        ]

        self.key_cache={}
        self.sess = None
        self.story_meta = None
        self.current_processes = []

    def refresh_auth_token(self):
        logger.info("Refreshing auth token...")
        for attempt in range(5):
            try:
                token_dict = self.auth_token()
                auth_token = token_dict.get('auth-token')
                if auth_token:
                    self.token = token_dict
                    self.header["authorization"] = f"Bearer {auth_token}"
                    logger.info("Auth token successfully refreshed!")
                    return True
            except Exception as e:
                logger.error(f"Failed to refresh auth token (attempt {attempt+1}): {e}")
            time.sleep(3 * (attempt + 1))
        return False

    def auth_token(self):
        url = Config.PFM_WEB_BASE
        header={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            response = req.head(url, headers=header, timeout=10)
            self.cookies_jar = response.cookies
            res = response.headers.get("set-cookie", "")
            self.raw_cookies = res 
            if not res:
                return {}
            token=[i.strip().split(";")[0].split("=") for i in res.split(",") if "auth-token" in i]
            return dict(token)
        except Exception as e:
            logger.error(f"Error fetching auth token: {e}")
            return {}

    async def get_session(self):
        if self.sess is None or self.sess.closed:
            self.sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
        return self.sess

    async def _make_request(self, method, url, **kwargs):
        for attempt in range(5):
            try:
                session = await self.get_session()
                async with session.request(method, url, **kwargs) as res:
                    if res.status == 200:
                        data = await res.json()
                        try:
                            import json
                            import re
                            if not hasattr(self, 'last_debug_info'):
                                self.last_debug_info = {}
                            
                            sid = "unknown"
                            match = re.search(r'show_id[s]?=([a-f0-9]+)', url)
                            if match: sid = match.group(1)
                            elif "entity_id" in kwargs.get("json", {}): sid = kwargs["json"]["entity_id"]
                            
                            if sid not in self.last_debug_info:
                                self.last_debug_info[sid] = []
                                
                            headers_dict = kwargs.get('headers') or {}
                            body_dict = kwargs.get('json')
                            
                            curl_parts = [f"curl -X {method} '{url}'"]
                            for k, v in headers_dict.items():
                                curl_parts.append(f"-H '{k}: {v}'")
                            if body_dict:
                                body_str = json.dumps(body_dict).replace("'", "\\'")
                                curl_parts.append(f"-d '{body_str}'")
                            curl_str = " ".join(curl_parts)
                            
                            self.last_debug_info[sid].append({
                                "curl": curl_str,
                                "request": {
                                    "method": method,
                                    "url": url,
                                    "headers": headers_dict,
                                    "body": body_dict
                                },
                                "response": data
                            })
                        except Exception as debug_e:
                            logger.error(f"Debug log error: {debug_e}")
                            
                        return data
                    elif res.status in [401, 403]:
                        logger.warning(f"Unauthorized (status {res.status}). Refreshing token...")
                        self.refresh_auth_token()
                        if "headers" in kwargs:
                            kwargs["headers"] = self.header.copy()
                    elif res.status == 429:
                        wait = int(res.headers.get("Retry-After", 5))
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"Request failed with status {res.status}: {url}")
                        if res.status < 500:
                            return None
            except Exception as e:
                logger.error(f"Request Error (Attempt {attempt+1}): {e}")
                if "closed" in str(e).lower():
                    self.sess = None # Force re-creation
            await asyncio.sleep(3 * (attempt + 1))
        return None

    async def get_detail(self, show_id, seq, info_level='max'):
        custom_headers = {
            "version-name": "9.1.3",
            "platform-version": "29",
            "app-version": "2013",
            "authorization": f"Bearer {self.token.get('auth-token', '')}"
        }
        
        data = await self._make_request(
            'GET',
            f'{self.base}/v2/content_api/show.get_details?show_id={show_id}&curr_ptr={seq-1}&info_level={info_level}',
            headers=custom_headers
        )
        
        if data and data.get("status") == 1:
            res_list = data.get("result", [])
            if res_list:
                item = res_list[0]
                data["result"] = {
                    "show_title": item.get("show_title"),
                    "stories": item.get("stories", [])
                }
        return data

    async def get_show_info(self, show_id, info_level='max'):
        custom_headers = {
            "version-name": "9.1.3",
            "platform-version": "29",
            "app-version": "2013",
            "authorization": f"Bearer {self.token.get('auth-token', '')}"
        }
        
        # Hit the API 5 times concurrently and find the max episodes_count
        tasks = []
        for _ in range(5):
            tasks.append(self._make_request(
                'GET',
                f'{self.base}/v2/content_api/show.get_details?show_id={show_id}&curr_ptr=0&info_level={info_level}',
                headers=custom_headers
            ))
        
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        max_episodes_count = 0
        best_details = None
        
        for details in responses:
            if isinstance(details, dict) and details.get("status") == 1:
                res_list = details.get("result", [])
                if res_list:
                    item = res_list[0]
                    count = item.get("episodes_count", 0)
                    if count >= max_episodes_count:
                        max_episodes_count = count
                        best_details = details
        
        # Grab the HD Image from the old endpoint
        hd_image = None
        old_data = await self._make_request(
            'GET',
            f'{self.base}{self.endpoint[2]}?show_id={show_id}&curr_ptr=0',
            headers=self.header
        )
        if old_data and old_data.get("status") == 1:
            res = old_data.get("result", {})
            stories = res.get("stories", [])
            if stories:
                hd_image = stories[0].get("image_url")
        
        if best_details and best_details.get("status") == 1:
            res_list = best_details.get("result", [])
            if res_list:
                item = res_list[0]
                return {
                    "title": item.get("show_title"),
                    "total_episodes": max_episodes_count,
                    "show_id": show_id,
                    "image": hd_image if hd_image else item.get("image_url"),
                    "language": item.get("language", "Unknown").capitalize()
                }
        return None
           
    async def get_story(self,show_id):
        return await self._make_request(
            'GET',
            f'{self.base}{self.endpoint[0]}?show_ids={show_id}',
            headers=self.header
        )
        
    async def add_story(self,action):
        if not self.story_meta: return
        uid,show_id,story_id = self.story_meta
        h = self.header.copy(); h.update({'content-type': 'application/json'})
        d = {'action': action, 'creator_uid': f"{uid}", 'entity_id': f"{show_id}",
             'entity_type': 'show', 'progress_action': '', 'source': 'player', 'story_id': f"{story_id}"}
        
        return await self._make_request(
            'POST',
            f'{self.base}{self.endpoint[1]}',
            headers=h,
            json=d
        )
           
    async def get_pssh(self,url):
        for attempt in range(3):
            try:
                session = await self.get_session()
                async with session.get(url, headers=self.header) as res:
                    if res.status == 200:
                        txt = await res.text()
                        pssh=re.search(
                            r"edef8ba9-79d6-4ace-a3c8-27dcd51d21ed.*?<cenc:pssh>(.*?)</cenc:pssh>",
                            txt,
                            re.DOTALL
                        ).group(1)

                        kid=re.search(
                            r'(?:cenc:)?default_KID="([^"]+)"',
                            txt
                        ).group(1).replace("-","")
                        return kid,pssh
            except Exception as e:
                logger.error(f"PSSH Error (Attempt {attempt+1}): {e}")
            await asyncio.sleep(2)
        return None, None

    def decrypt(self,j:int,device_id:str,cipher:str)->str:

        data=f"{device_id}:{j}".encode()

        encoded=base64.b64encode(data).decode()

        chars=list(encoded)

        for i in range(0,len(chars)-1,2):
            chars[i],chars[i+1]=chars[i+1],chars[i]

        key="".join(chars).encode()

        d=base64.b64decode(cipher)

        salt=d[:16]
        iv=d[16:28]
        str_data=d[28:-16]

        key=hashlib.pbkdf2_hmac("sha256",key,salt,10000,dklen=32)

        cipher_obj=AES.new(key,AES.MODE_GCM,nonce=iv)

        plaintext=cipher_obj.decrypt(str_data)

        return plaintext

    async def get_license(self,show_id):
        header = self.header.copy()
        header.update({
            "client-ts": str(Config.PFM_CLIENT_TS),
            "content-type":"application/json;charset=utf-8"
        })
       
        res = await self._make_request(
            'GET',
            f"{self.base}{self.endpoint[3]}?show_id={show_id}",
            headers=header
        )
        if res:
            return res.get("blob")
        return None

    async def license(self,show_id):
        data = await self.get_license(show_id)
        if not data: return None
        wv_lic=self.decrypt(Config.PFM_CLIENT_TS, Config.PFM_DEVICE_ID, data)
        return wv_lic.decode()

    async def get_keys(self,mpd_url,show_id,wvd_path="l3.wvd"):
        if mpd_url in self.key_cache:
            return self.key_cache[mpd_url]

        lic_url = await self.license(show_id)
        if not lic_url: return None, None

        kid, pssh = await self.get_pssh(mpd_url)
        if not pssh: return None, None
       
        device=Device.load(wvd_path)
        cdm=Cdm.from_device(device)
        session=cdm.open()
        challenge=cdm.get_license_challenge(session,PSSH(pssh))
        
        try:
            sess = await self.get_session()
            async with sess.post(lic_url,data=challenge) as response:
                license_data = await response.content.read()
                cdm.parse_license(session, license_data)
        except Exception as e:
            logger.error(f"License Request Error: {e}")
            return None, None

        keys={}
        for key in cdm.get_keys(session):
            keys[key.kid.hex]=key.key.hex()

        cdm.close(session)
        key = keys.get(kid)
        if key:
            result=(kid, key)
            self.key_cache[mpd_url]=result
            return result
        return None, None

    async def download_episodes(self,show_id,seq,end,output_dir,progress_callback=None,cancel_flag=None,on_complete=None,on_start=None,quality="192", discovery_done=None, info_level='max', process_tracker=None, on_retry=None):
        total_target = end - seq + 1
        files=[]
        self.last_download_error = None
        abort_reason = None
        
        # Qualities to try in order (lowest first for faster downloads)
        qualities = ["32", "64", "128", quality]
        # Remove duplicates and keep order
        qualities = list(dict.fromkeys([q for q in qualities if q]))
        
        # Queue for metadata discovered
        queue = asyncio.Queue(maxsize=100)
        
        async def worker():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if cancel_flag and cancel_flag():
                        break
                    continue
                
                if item is None:
                    queue.task_done()
                    break
                
                if cancel_flag and cancel_flag():
                    queue.task_done()
                    continue
                
                seq_num, ep = item
                try:
                    raw_name = ep[0].strip()
                    if on_start:
                        try:
                            if asyncio.iscoroutinefunction(on_start): await on_start(seq_num, raw_name)
                            else: on_start(seq_num, raw_name)
                        except: pass
                        
                    name = re.sub(r'[\\/*?\"<>|#@!\[\](){}^~`$%&+=;:]+', '', raw_name)
                    name = re.sub(r'[_*]+', '', name)
                    name = re.sub(r'\s+', ' ', name).strip()
                    
                    mpd = ep[1]
                    duration = ep[3] if len(ep) > 3 else 0
                    video_url = ep[4] if len(ep) > 4 else None
                    
                    clean_name = re.sub(r'^(?:(?:Ep|Episode|E|Ch|Chapter|C)[\s\-.:,]*\d+[\s\-.:,]*)+', '', name, flags=re.IGNORECASE).strip()
                    clean_name = re.sub(r'^\d+[\s\-.:,]+', '', clean_name).strip()
                    if clean_name:
                        filename = f"Ep {seq_num} - {clean_name}.m4a"
                    else:
                        filename = f"Ep {seq_num}.m4a"
                    
                    show_title_cleaned = re.sub(r'[\\/*?"<>|#@!\[\](){}^~`$%&+=;:_*]+', '', self.current_show_title).strip()
                    show_dir = os.path.join(output_dir, show_title_cleaned)
                    os.makedirs(show_dir, exist_ok=True)
                    m4a = os.path.join(show_dir, filename)
                    
                    if os.path.exists(m4a) and os.path.getsize(m4a) > 10000:
                        # Already downloaded, get duration
                        dur = 0
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", m4a,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                            )
                            out, _ = await proc.communicate()
                            dur = int(float(out.decode().strip()))
                        except:
                            dur = int(duration) if duration else 0
                        
                        if on_complete:
                            await on_complete(seq_num, m4a, dur)
                        files.append((seq_num, m4a, dur))
                    else:
                        download_success = False
                        
                        for overall_attempt in range(5):
                            if cancel_flag and cancel_flag(): break
                            if download_success: break
                            
                            # Send retry notification (from 2nd attempt onwards)
                            if overall_attempt > 0 and on_retry:
                                try:
                                    if asyncio.iscoroutinefunction(on_retry): await on_retry(seq_num, overall_attempt + 1)
                                    else: on_retry(seq_num, overall_attempt + 1)
                                except: pass
                            
                            # --- Type 2: Try video MPD audio (non-DRM, priority 1) ---
                            if video_url and not download_success:
                                try:
                                    session = await self.get_session()
                                    async with session.get(video_url) as resp:
                                        if resp.status == 200:
                                            mpd_text = await resp.text()
                                            audio_match = re.search(
                                                r'contentType="audio".*?<BaseURL>([^<]+)</BaseURL>',
                                                mpd_text, re.DOTALL
                                            )
                                            if audio_match:
                                                audio_filename = audio_match.group(1)
                                                audio_url = video_url.rsplit("/", 1)[0] + "/" + audio_filename
                                                
                                                proc = await asyncio.create_subprocess_exec(
                                                    "ffmpeg", "-y", "-loglevel", "error",
                                                    "-i", audio_url,
                                                    "-vn", "-c:a", "copy", m4a
                                                )
                                                self.current_processes.append(proc)
                                                if process_tracker is not None:
                                                    process_tracker.append(proc)
                                                try:
                                                    await asyncio.wait_for(proc.wait(), timeout=300)
                                                except:
                                                    try: proc.kill(); await proc.wait()
                                                    except: pass
                                                finally:
                                                    if proc in self.current_processes:
                                                        self.current_processes.remove(proc)
                                                    if process_tracker is not None and proc in process_tracker:
                                                        process_tracker.remove(proc)
                                                
                                                if os.path.exists(m4a) and os.path.getsize(m4a) > 10000:
                                                    dur = 0
                                                    try:
                                                        pr = await asyncio.create_subprocess_exec(
                                                            "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                            "-of", "default=noprint_wrappers=1:nokey=1", m4a,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                                                        )
                                                        out, _ = await pr.communicate()
                                                        dur = int(float(out.decode().strip()))
                                                    except:
                                                        dur = int(duration) if duration else 0
                                                    
                                                    if on_complete:
                                                        await on_complete(seq_num, m4a, dur)
                                                    files.append((seq_num, m4a, dur))
                                                    download_success = True
                                                    logger.info(f"Success Ep.{seq_num} via video MPD audio (attempt {overall_attempt+1})")
                                                    break
                                except Exception as e:
                                    self.last_download_error = str(e)
                                    logger.error(f"Type 2 download Ep.{seq_num} (attempt {overall_attempt+1}): {e}")
                            
                            # --- Type 1: DRM audio fallback (priority 2) ---
                            if not download_success:
                                for q in qualities:
                                    if download_success: break
                                    link = mpd.rsplit("/", 1)[0] + f"/protected_audio_mpd_{q}k.mp4"
                                    try:
                                        _, key = await self.get_keys(mpd, show_id)
                                        if not key:
                                            logger.warning(f"No key for Ep.{seq_num} (attempt {overall_attempt+1})")
                                            continue

                                        proc = await asyncio.create_subprocess_exec(
                                            "ffmpeg", "-y", "-loglevel", "error",
                                            "-decryption_key", key, "-i", link,
                                            "-map", "0:a:0", "-vn", "-c:a", "copy", m4a
                                        )
                                        self.current_processes.append(proc)
                                        if process_tracker is not None:
                                            process_tracker.append(proc)
                                        try:
                                            await asyncio.wait_for(proc.wait(), timeout=300)
                                        except:
                                            try: proc.kill(); await proc.wait()
                                            except: pass
                                        finally:
                                            if proc in self.current_processes:
                                                self.current_processes.remove(proc)
                                            if process_tracker is not None and proc in process_tracker:
                                                process_tracker.remove(proc)

                                        if os.path.exists(m4a) and os.path.getsize(m4a) > 10000:
                                            dur = 0
                                            try:
                                                pr = await asyncio.create_subprocess_exec(
                                                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                    "-of", "default=noprint_wrappers=1:nokey=1", m4a,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                                                )
                                                out, _ = await pr.communicate()
                                                dur = int(float(out.decode().strip()))
                                            except:
                                                dur = int(duration) if duration else 0
                                            
                                            if on_complete:
                                                await on_complete(seq_num, m4a, dur)
                                            files.append((seq_num, m4a, dur))
                                            download_success = True
                                            logger.info(f"Success Ep.{seq_num} at {q}k DRM (attempt {overall_attempt+1})")
                                            break
                                    except Exception as e:
                                        self.last_download_error = str(e)
                                        logger.error(f"DRM Error Ep.{seq_num} ({q}k, attempt {overall_attempt+1}): {e}")
                            
                            if not download_success:
                                await asyncio.sleep(2)
                        
                        if not download_success:
                            logger.error(f"FAILED Ep.{seq_num} after 5 attempts.")
                            if on_complete:
                                await on_complete(seq_num, None, 0)
                finally:
                    queue.task_done()

        # Start Workers
        num_workers = 1
        workers = [asyncio.create_task(worker()) for _ in range(num_workers)]
        self.active_workers = workers
        
        self.current_show_title = "PocketFM"
        current_seq = seq
        processed_metadata = set()
        empty_page_retries = 0
        api_fail_retries = 0

        while current_seq <= end:
            if cancel_flag and cancel_flag(): break
            
            story_data = await self.get_detail(show_id, current_seq, info_level=info_level)
            if not story_data or story_data.get("status") != 1:
                api_fail_retries += 1
                if api_fail_retries < 3:
                    # Retry same cursor position after a small delay
                    logger.warning(f"API failed for curr_ptr={current_seq-1} (retry {api_fail_retries}/3)")
                    await asyncio.sleep(3)
                    continue
                else:
                    # After 3 retries, skip ahead by 1 and reset counter
                    logger.error(f"API failed 3 times for curr_ptr={current_seq-1}. Advancing cursor.")
                    api_fail_retries = 0
                    current_seq += 1
                    continue
            
            api_fail_retries = 0  # Reset on success
            
            result = story_data.get("result", {})
            self.current_show_title = result.get("show_title", self.current_show_title)
            stories = result.get("stories", [])
            
            if not stories:
                empty_page_retries += 1
                if empty_page_retries < 3:
                    # Could be a temporary API glitch, retry after delay
                    logger.warning(f"Empty stories at curr_ptr={current_seq-1} (retry {empty_page_retries}/3)")
                    await asyncio.sleep(2)
                    continue
                else:
                    # 3 consecutive empty results - likely end of show
                    logger.info(f"Got 3 consecutive empty pages at curr_ptr={current_seq-1}. Assuming end of episodes.")
                    break
            
            empty_page_retries = 0  # Reset on non-empty
            
            mapping = {}
            to_sub_count = 0
            
            for i in stories:
                s = i.get("natural_sequence_number", 0)
                if seq <= s <= end and s not in processed_metadata:
                    mapping[i.get('seq_number')] = s
                    media = i.get("media_url_enc", "")
                    if media:
                        video_url = (i.get("video_info") or {}).get("android", {}).get("video_url")
                        info = (i.get("story_title"), media, s, i.get("duration"), video_url)
                        await queue.put((s, info))
                        processed_metadata.add(s)
                        if progress_callback:
                            try:
                                if asyncio.iscoroutinefunction(progress_callback): await progress_callback(s)
                                else: progress_callback(s)
                            except: pass
                    else:
                        self.story_meta = [i.get('created_by'), show_id, i.get('story_id')]
                        await self.add_story('subscribe_story')
                        to_sub_count += 1
            
            if to_sub_count > 0:
                await asyncio.sleep(2)
                saved_data = await self.get_story(show_id)
                saved_stories = (saved_data.get("result", {}) if saved_data else {}).get("stories", [])
                for i in saved_stories:
                    s = mapping.get(i.get('seq_number'))
                    if s and s not in processed_metadata:
                        media = i.get("media_url_enc", "")
                        if media:
                            video_url = (i.get("video_info") or {}).get("android", {}).get("video_url")
                            info = (i.get("story_title"), media, s, i.get("duration"), video_url)
                            await queue.put((s, info))
                            processed_metadata.add(s)
                            if progress_callback:
                                try:
                                    if asyncio.iscoroutinefunction(progress_callback): await progress_callback(s)
                                    else: progress_callback(s)
                                except: pass
            
            for mapped_seq, mapped_s in mapping.items():
                if mapped_s not in processed_metadata:
                    logger.warning(f"Metadata permanently missing for Ep.{mapped_s}. Skipping...")
                    processed_metadata.add(mapped_s)
                    if progress_callback:
                        try:
                            if asyncio.iscoroutinefunction(progress_callback): await progress_callback(mapped_s)
                            else: progress_callback(mapped_s)
                        except: pass
                    if on_complete:
                        try:
                            if asyncio.iscoroutinefunction(on_complete): await on_complete(mapped_s, None, 0)
                            else: on_complete(mapped_s, None, 0)
                        except: pass

            # Advance cursor using max natural_sequence_number seen in this batch
            # This prevents cursor drift when sequence numbers don't align with page offsets
            max_nat_seq = max((i.get("natural_sequence_number", 0) for i in stories), default=current_seq)
            cursor_advance = len(stories)
            seq_advance = max_nat_seq - current_seq + 1
            # Use whichever advances further to avoid getting stuck or re-fetching
            current_seq += max(cursor_advance, seq_advance)

        # --- Gap-Filling Pass ---
        # After main pagination, check for any episodes in [seq, end] that were never discovered
        # and retry fetching them individually
        if not (cancel_flag and cancel_flag()):
            missing_episodes = [ep_num for ep_num in range(seq, end + 1) if ep_num not in processed_metadata]
            if missing_episodes:
                logger.info(f"Gap-filling: {len(missing_episodes)} episodes missed in main pass. Retrying...")
                
                # Process missing episodes in small batches by fetching pages around them
                gap_cursors_tried = set()
                for miss_seq in missing_episodes:
                    if cancel_flag and cancel_flag(): break
                    if miss_seq in processed_metadata: continue  # Could have been found in a previous gap-fill iteration
                    
                    # Avoid re-fetching the same cursor position
                    cursor_pos = miss_seq
                    if cursor_pos in gap_cursors_tried: continue
                    gap_cursors_tried.add(cursor_pos)
                    
                    story_data = None
                    for _ in range(5):
                        story_data = await self.get_detail(show_id, cursor_pos, info_level=info_level)
                        if story_data and story_data.get("status") == 1:
                            result = story_data.get("result", {})
                            stories = result.get("stories", [])
                            found = False
                            for i in stories:
                                if i.get("natural_sequence_number", 0) == miss_seq:
                                    found = True
                                    break
                            if found:
                                break
                        await asyncio.sleep(1)
                        
                    if not story_data or story_data.get("status") != 1:
                        continue
                    
                    result = story_data.get("result", {})
                    stories = result.get("stories", [])
                    if not stories: continue
                    
                    gap_sub_count = 0
                    gap_mapping = {}
                    
                    for i in stories:
                        s = i.get("natural_sequence_number", 0)
                        # Mark this cursor position as tried for all episodes in this page
                        gap_cursors_tried.add(s)
                        if seq <= s <= end and s not in processed_metadata:
                            gap_mapping[i.get('seq_number')] = s
                            media = i.get("media_url_enc", "")
                            if media:
                                video_url = (i.get("video_info") or {}).get("android", {}).get("video_url")
                                info = (i.get("story_title"), media, s, i.get("duration"), video_url)
                                await queue.put((s, info))
                                processed_metadata.add(s)
                                if progress_callback:
                                    try:
                                        if asyncio.iscoroutinefunction(progress_callback): await progress_callback(s)
                                        else: progress_callback(s)
                                    except: pass
                            else:
                                self.story_meta = [i.get('created_by'), show_id, i.get('story_id')]
                                await self.add_story('subscribe_story')
                                gap_sub_count += 1
                    
                    if gap_sub_count > 0:
                        await asyncio.sleep(2)
                        saved_data = await self.get_story(show_id)
                        saved_stories = (saved_data.get("result", {}) if saved_data else {}).get("stories", [])
                        for i in saved_stories:
                            s = gap_mapping.get(i.get('seq_number'))
                            if s and s not in processed_metadata:
                                media = i.get("media_url_enc", "")
                                if media:
                                    video_url = (i.get("video_info") or {}).get("android", {}).get("video_url")
                                    info = (i.get("story_title"), media, s, i.get("duration"), video_url)
                                    await queue.put((s, info))
                                    processed_metadata.add(s)
                                    if progress_callback:
                                        try:
                                            if asyncio.iscoroutinefunction(progress_callback): await progress_callback(s)
                                            else: progress_callback(s)
                                        except: pass
                    
                    for mapped_seq, mapped_s in gap_mapping.items():
                        if mapped_s not in processed_metadata:
                            logger.warning(f"Gap-fill: Ep.{mapped_s} still missing after retry.")
                            processed_metadata.add(mapped_s)
                            if progress_callback:
                                try:
                                    if asyncio.iscoroutinefunction(progress_callback): await progress_callback(mapped_s)
                                    else: progress_callback(mapped_s)
                                except: pass
                            if on_complete:
                                try:
                                    if asyncio.iscoroutinefunction(on_complete): await on_complete(mapped_s, None, 0)
                                    else: on_complete(mapped_s, None, 0)
                                except: pass
                
                still_missing = [ep_num for ep_num in range(seq, end + 1) if ep_num not in processed_metadata]
                if still_missing:
                    logger.warning(f"After gap-fill, {len(still_missing)} episodes still missing: {still_missing[:20]}...")
                    
                    # Send "Episode n not found..." for each missing episode
                    # Track consecutive not-found for abort
                    consecutive_not_found = 0
                    for miss_ep in sorted(still_missing):
                        if cancel_flag and cancel_flag(): break
                        
                        consecutive_not_found += 1
                        processed_metadata.add(miss_ep)
                        
                        if on_complete:
                            try:
                                if asyncio.iscoroutinefunction(on_complete): await on_complete(miss_ep, None, 0, "not_found")
                                else: on_complete(miss_ep, None, 0, "not_found")
                            except: pass
                        
                        # Check if 3 consecutive episodes are not found
                        if consecutive_not_found >= 3:
                            abort_reason = "many_not_found"
                            logger.error(f"3 consecutive episodes not found. Aborting download for show {show_id}.")
                            break
                        
                        # Reset consecutive counter if there's a found episode between missing ones
                        # (check if the next expected episode was already processed)
                        next_ep = miss_ep + 1
                        if next_ep in processed_metadata and next_ep not in still_missing:
                            consecutive_not_found = 0

        # Signal that metadata discovery is complete
        if discovery_done:
            discovery_done.set()

        # Signal Workers to Stop
        for _ in range(num_workers):
            await queue.put(None)
        
        await asyncio.gather(*workers)
        files.sort(key=lambda x: x[0])
        return {"success": len(files) > 0, "files": files, "total": total_target, "abort_reason": abort_reason}

