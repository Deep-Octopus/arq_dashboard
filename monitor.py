import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import asyncio
import pickle
import logging
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from arq.constants import default_queue_name, result_key_prefix

# ================= 配置区域 =================
# 请根据你的实际情况修改 Redis 配置
REDIS_SETTINGS = {
    "host": "localhost",  # 如果是 docker 部署，可能需要改为 docker 内部 IP 或 host.docker.internal
    "port": 6379,
    "password": "", # 你的密码
    "db": 0
}
SCAN_COUNT = 100  # 每次扫描 Redis 的 key 数量
MAX_HISTORY = 50  # 前端仅展示最近的 50 条记录
# ===========================================

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ARQ-Monitor")

apps = FastAPI(title="ARQ Dashboard")

# 挂载静态文件目录
apps.mount("/static", StaticFiles(directory="static"), name="static")

# 数据模型
class JobModel(BaseModel):
    job_id: str
    function: str
    status: str      # 'complete', 'failed', 'queued'
    success: bool
    args: str        # 参数的字符串表示
    result: str      # 结果或错误信息
    start_time: Optional[str]
    finish_time: Optional[str]
    duration: Optional[str]
    enqueue_time: Optional[str]

class DashboardStats(BaseModel):
    queued_count: int
    jobs: List[JobModel]

async def get_redis_client() -> Redis:
    return Redis(**REDIS_SETTINGS)

# === 时间戳转换 ===
def parse_timestamp(ts):
    """将毫秒级时间戳转换为 datetime 对象，用于显示"""
    if ts is None:
        return None
    try:
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, (int, float)):
            # 13位整数是毫秒，需要 / 1000
            return datetime.fromtimestamp(ts / 1000.0)
    except Exception:
        return None
    return None

# === 安全的时长计算 ===
def calculate_duration(start, finish):
    """
    计算耗时，兼容 datetime 对象和 int(毫秒)
    """
    if not start or not finish:
        return "N/A"
    
    try:
        # 情况1: 两个都是 datetime 对象
        if isinstance(start, datetime) and isinstance(finish, datetime):
            delta = finish - start
            return f"{delta.total_seconds():.2f}s"
        
        # 情况2: 两个都是整数 (毫秒时间戳)
        # 你的报错就是因为走了这里，但之前代码试图调用 .total_seconds()
        if isinstance(start, (int, float)) and isinstance(finish, (int, float)):
            delta_ms = finish - start
            return f"{delta_ms / 1000.0:.2f}s"
            
        # 情况3: 混合类型 (尝试都转成 float 时间戳)
        ts_start = start.timestamp() if isinstance(start, datetime) else start / 1000.0
        ts_finish = finish.timestamp() if isinstance(finish, datetime) else finish / 1000.0
        return f"{(ts_finish - ts_start):.2f}s"

    except Exception as e:
        logger.error(f"Duration calc error: {e}")
        return "Error"

@apps.get("/")
async def read_index():
    return FileResponse('static/index.html')

@apps.get("/api/data", response_model=DashboardStats)
async def get_dashboard_data():
    redis = await get_redis_client()
    try:
        # 1. 获取排队中的任务数量
        queued_count = await redis.zcard(default_queue_name)
        
        
        # 2. 扫描最近的结果 Key (arq:result:*)
        # 你的服务器很快，但为了安全，我们还是用 SCAN 迭代
        cursor = 0
        keys = []
        # 只扫描一次 batch，或者循环扫描直到找到足够的 key
        # 这里为了演示效率，简单扫描匹配的前 200 个 key
        # 在生产海量数据下，你可能需要优化这部分逻辑
        cursor, batch_keys = await redis.scan(cursor=0, match=f"{result_key_prefix}*", count=SCAN_COUNT)
        keys.extend(batch_keys)
        # 如果 key 太多，截取一部分最新的（这里仅作简单截取，Redis scan 顺序不保证时间）
        # 真正的时间排序需要在内存里做
        process_keys = keys[:MAX_HISTORY]
        
        jobs_data = []
        if process_keys:
            # 批量获取 Values
            values = await redis.mget(process_keys)
            
            for key, value in zip(process_keys, values):
                if not value:
                    continue
                
                try:
                    # ARQ 存储的是 pickle 序列化对象
                    # 注意：如果你的任务参数包含自定义 Class，这里可能会反序列化失败
                    # 除非此脚本也能 import 这些 Class。
                    # 为了通用性，我们捕获错误。
                    data = pickle.loads(value)
                    
                    job_id = key.decode().replace(result_key_prefix, "")
                    
                    # 计算耗时
                    duration = "N/A"
                    if data.get("et", None) and data.get("ft", None):
                        duration = calculate_duration(data.get("et", None), data.get("ft", None))

                    jobs_data.append(JobModel(
                        id=data.get("id", None),
                        job_id=job_id,
                        function=data.get("f", None),
                        status="complete" if data.get("s", None) else "failed",
                        success=data.get("s", None),
                        args=str(data.get("a", None)) + " " + str(data.get("k", None)),
                        result=str(data.get("r", None)) if data.get("s", None) else str(data.get("error", None)),
                        enqueue_time=parse_timestamp(data.get("et", None)).strftime("%Y-%m-%d %H:%M:%S") if data.get("et", None) else "-",
                        start_time=parse_timestamp(data.get("st", None)).strftime("%Y-%m-%d %H:%M:%S") if data.get("st", None) else "-",
                        finish_time=parse_timestamp(data.get("ft", None)).strftime("%Y-%m-%d %H:%M:%S") if data.get("ft", None) else "-",
                        duration=duration,
                    ))
                except Exception as e:
                    logger.error(f"Error decoding job {key}: {e}")
                    # 添加一个错误的占位符
                    jobs_data.append(JobModel(
                        job_id=key.decode(), function="Unknown/DecodeError", status="failed",
                        success=False, args="-", result=f"Pickle Decode Error: {str(e)}",
                        start_time=None, finish_time=None, duration=None, enqueue_time=None
                    ))

        # 按开始时间倒序排序 (最新的在上面)
        jobs_data.sort(key=lambda x: x.start_time or "0", reverse=True)

        return DashboardStats(
            queued_count=queued_count,
            jobs=jobs_data
        )

    finally:
        await redis.aclose()

if __name__ == "__main__":
    import uvicorn
    # 监听 8081 端口，避免与你的主程序 8000 冲突
    uvicorn.run(apps, host="0.0.0.0", port=8999)