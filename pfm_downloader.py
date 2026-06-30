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
                            kwargs["headers"]["authorization"] = self.header.get("authorization", "")
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
        
        details = await self._make_request(
            'GET',
            f'{self.base}/v2/content_api/show.get_details?show_id={show_id}&curr_ptr=0&info_level={info_level}',
            headers=custom_headers
        )
        
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
        
        if details and details.get("status") == 1:
            res_list = details.get("result", [])
            if res_list:
                item = res_list[0]
                return {
                    "title": item.get("show_title"),
                    "total_episodes": item.get("episodes_count"),
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

    async def get_audio_url_from_video_mpd(self, mpd_url):
        """Fetch a video MPD and extract the audio BaseURL to build a direct audio download link."""
        for attempt in range(3):
            try:
                session = await self.get_session()
                async with session.get(mpd_url) as res:
                    if res.status == 200:
                        txt = await res.text()
                        # Find the audio AdaptationSet and extract its BaseURL
                        audio_match = re.search(
                            r'<AdaptationSet[^>]*contentType="audio"[^>]*>.*?<BaseURL>([^<]+)</BaseURL>',
                            txt,
                            re.DOTALL
                        )
                        if audio_match:
                            audio_filename = audio_match.group(1)
                            # Build full URL: replace the MPD filename with audio filename
                            base = mpd_url.rsplit("/", 1)[0]
                            audio_url = f"{base}/{audio_filename}"
                            logger.info(f"Extracted audio URL from video MPD: {audio_url}")
                            return audio_url
                        else:
                            logger.warning(f"No audio BaseURL found in video MPD: {mpd_url}")
                    else:
                        logger.warning(f"Video MPD fetch failed with status {res.status}: {mpd_url}")
            except Exception as e:
                logger.error(f"Video MPD Parse Error (Attempt {attempt+1}): {e}")
            await asyncio.sleep(2)
        return None

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

    async def download_episodes(self,show_id,seq,end,output_dir,progress_callback=None,cancel_flag=None,on_complete=None,on_start=None,quality="192", discovery_done=None, info_level='max', process_tracker=None):
        total_target = end - seq + 1
        files=[]
        self.last_download_error = None
        
        # Qualities to try in order
        qualities = [quality, "128", "64", "32"]
        # Remove duplicates and keep order
        qualities = list(dict.fromkeys([q for q in qualities if q]))
        
        # Queue for metadata discovered
        queue = asyncio.Queue(maxsize=100)
        
        async def worker():
            while True:
                if cancel_flag and cancel_flag():
                    break
                
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                if item is None:
                    queue.task_done()
                    break
                
                seq_num, ep = item
                try:
                    raw_name = ep[0].strip()
                    if on_start:
                        try:
                            if asyncio.iscoroutinefunction(on_start): await on_start(seq_num, raw_name)
                            else: on_start(seq_num, raw_name)
                        except: pass
                        
                    name = re.sub(r'[^a-zA-Z0-9\s\-]', '', raw_name)
                    name = re.sub(r'\s+', ' ', name).strip()
                    
                    mpd = ep[1]
                    duration = ep[3] if len(ep) > 3 else 0
                    video_mpd_url = ep[4] if len(ep) > 4 else None
                    
                    clean_name = re.sub(r'^(?:(?:Ep|Episode|E|Ch|Chapter|C)[\s\-.:,]*\d+[\s\-.:,]*)+', '', name, flags=re.IGNORECASE).strip()
                    clean_name = re.sub(r'^\d+[\s\-.:,]+', '', clean_name).strip()
                    if clean_name:
                        filename = f"Ep {seq_num} - {clean_name}.m4a"
                    else:
                        filename = f"Ep {seq_num}.m4a"
                    
                    show_title_cleaned = re.sub(r"[^a-zA-Z0-9\s\-]", "", self.current_show_title).strip()
                    show_dir = os.path.join(output_dir, show_title_cleaned)
                    os.makedirs(show_dir, exist_ok=True)
                    m4a = os.path.join(show_dir, filename)
                    
                    if os.path.exists(m4a) and os.path.getsize(m4a) > 10000:
                        # (Existing ffprobe logic...)
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
                        
                        # --- Type 2: Video MPD audio download (Priority 1) ---
                        if video_mpd_url:
                            logger.info(f"Ep.{seq_num}: Trying Type 2 (video MPD audio)...")
                            for attempt in range(3):
                                if cancel_flag and cancel_flag(): break
                                try:
                                    audio_url = await self.get_audio_url_from_video_mpd(video_mpd_url)
                                    if not audio_url:
                                        logger.warning(f"No audio URL from video MPD for Ep.{seq_num} (Attempt {attempt+1})")
                                        await asyncio.sleep(2)
                                        continue
                                    
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
                                        logger.info(f"Success Ep.{seq_num} via Type 2 (video MPD audio)")
                                        break
                                except Exception as e:
                                    self.last_download_error = str(e)
                                    logger.error(f"Type 2 Error Ep.{seq_num} (Attempt {attempt+1}): {e}")
                                await asyncio.sleep(1)
                        
                        # --- Type 1: DRM MPD download (Fallback) ---
                        if not download_success and mpd:
                            if video_mpd_url:
                                logger.info(f"Ep.{seq_num}: Type 2 failed, falling back to Type 1 (DRM)...")
                            for q in qualities:
                                if download_success: break
                                link = mpd.rsplit("/", 1)[0] + f"/protected_audio_mpd_{q}k.mp4"
                                
                                for attempt in range(3): # 3 attempts per quality
                                    if cancel_flag and cancel_flag(): break
                                    try:
                                        _, key = await self.get_keys(mpd, show_id)
                                        if not key: 
                                            logger.warning(f"No key for Ep.{seq_num} (Attempt {attempt+1})")
                                            await asyncio.sleep(2)
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
                                            logger.info(f"Success Ep.{seq_num} at {q}k")
                                            break
                                    except Exception as e:
                                        self.last_download_error = str(e)
                                        logger.error(f"Worker Error Ep.{seq_num} (Quality {q}k, Attempt {attempt+1}): {e}")
                                    await asyncio.sleep(1)
                        
                        if not download_success:
                            logger.error(f"FAILED Ep.{seq_num} after all methods and retries.")
                            if on_complete:
                                await on_complete(seq_num, None, 0)
                finally:
                    queue.task_done()

        # Start Workers
        num_workers = 1
        workers = [asyncio.create_task(worker()) for _ in range(num_workers)]
        
        self.current_show_title = "PocketFM"
        current_seq = seq
        processed_metadata = set()

        while current_seq <= end:
            if cancel_flag and cancel_flag(): break
            
            story_data = await self.get_detail(show_id, current_seq, info_level=info_level)
            if not story_data or story_data.get("status") != 1:
                current_seq += 1
                continue
            
            result = story_data.get("result", {})
            self.current_show_title = result.get("show_title", self.current_show_title)
            stories = result.get("stories", [])
            if not stories: break
            
            mapping = {}
            to_sub_count = 0
            
            for i in stories:
                s = i.get("natural_sequence_number", 0)
                if seq <= s <= end and s not in processed_metadata:
                    mapping[i.get('seq_number')] = s
                    media = i.get("media_url_enc", "")
                    # Extract video_info.android.video_url for Type 2 (priority)
                    video_info = i.get("video_info", {})
                    android_video_url = video_info.get("android", {}).get("video_url", "") if video_info else ""
                    if media or android_video_url:
                        info = (i.get("story_title"), media, s, i.get("duration"), android_video_url)
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
                        video_info = i.get("video_info", {})
                        android_video_url = video_info.get("android", {}).get("video_url", "") if video_info else ""
                        if media or android_video_url:
                            info = (i.get("story_title"), media, s, i.get("duration"), android_video_url)
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
                    title = next((i.get("story_title", "") for i in stories if i.get("natural_sequence_number") == mapped_s), "")
                    processed_metadata.add(mapped_s)
                    if progress_callback:
                        try:
                            if asyncio.iscoroutinefunction(progress_callback): await progress_callback(mapped_s)
                            else: progress_callback(mapped_s)
                        except: pass
                    if on_start:
                        try:
                            if asyncio.iscoroutinefunction(on_start): await on_start(mapped_s, title)
                            else: on_start(mapped_s, title)
                        except: pass
                    if on_complete:
                        try:
                            if asyncio.iscoroutinefunction(on_complete): await on_complete(mapped_s, None, 0)
                            else: on_complete(mapped_s, None, 0)
                        except: pass

            # Safe increment: advance based on the maximum seq_number returned by the API
            max_seq = max((i.get('seq_number', 0) for i in stories), default=0)
            if max_seq >= current_seq:
                current_seq = max_seq + 1
            else:
                current_seq += len(stories)

        # Signal that metadata discovery is complete
        if discovery_done:
            discovery_done.set()

        # Signal Workers to Stop
        for _ in range(num_workers):
            await queue.put(None)
        
        await asyncio.gather(*workers)
        files.sort(key=lambda x: x[0])
        return {"success": len(files) > 0, "files": files, "total": total_target}
        return {"success": len(files) > 0, "files": files, "total": total_target}

