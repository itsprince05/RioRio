import os
import math
import asyncio
import logging
from telethon import TelegramClient
from telethon.tl.types import InputFileBig, InputFile
from telethon.tl.functions.upload import SaveFilePartRequest, SaveBigFilePartRequest
import random

logger = logging.getLogger(__name__)

class FastTelethon:
    @staticmethod
    async def upload_file(client: TelegramClient, file_path: str, progress_callback=None, workers: int = 16):
        """
        Uploads a file to Telegram using parallel chunking for maximum speed.
        """
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)
        
        # 512KB is the max chunk size allowed by Telegram
        part_size = 512 * 1024 
        part_count = math.ceil(file_size / part_size)
        is_big = file_size > 10 * 1024 * 1024
        
        # Generate a random 64-bit ID for the file
        file_id = random.getrandbits(63)
        
        uploaded_bytes = 0
        sem = asyncio.Semaphore(workers)
        
        async def upload_part(part_index, chunk):
            nonlocal uploaded_bytes
            async with sem:
                for attempt in range(3):
                    try:
                        if is_big:
                            await client(SaveBigFilePartRequest(
                                file_id=file_id, 
                                file_part=part_index, 
                                file_total_parts=part_count, 
                                bytes=chunk
                            ))
                        else:
                            await client(SaveFilePartRequest(
                                file_id=file_id, 
                                file_part=part_index, 
                                bytes=chunk
                            ))
                        
                        uploaded_bytes += len(chunk)
                        if progress_callback:
                            try:
                                if asyncio.iscoroutinefunction(progress_callback):
                                    await progress_callback(uploaded_bytes, file_size)
                                else:
                                    progress_callback(uploaded_bytes, file_size)
                            except:
                                pass
                        break
                    except Exception as e:
                        logger.warning(f"Upload part {part_index} failed (attempt {attempt+1}): {e}")
                        await asyncio.sleep(1 * (attempt + 1))
                        if attempt == 2:
                            raise e

        tasks = []
        with open(file_path, "rb") as f:
            for i in range(part_count):
                chunk = f.read(part_size)
                tasks.append(asyncio.create_task(upload_part(i, chunk)))
                
        await asyncio.gather(*tasks)
        
        if is_big:
            return InputFileBig(id=file_id, parts=part_count, name=file_name)
        else:
            return InputFile(id=file_id, parts=part_count, name=file_name, md5_checksum="")
