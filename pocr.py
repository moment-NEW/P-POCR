#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P-POCR: PaddleOCR-VL PDF → Markdown 转换工具
开箱即用，支持高级配置扩展
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    import requests
except ImportError:
    print("错误: 请先安装 requests 库")
    print("运行: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("错误: 请先安装 PyYAML 库")
    print("运行: pip install PyYAML")
    sys.exit(1)


class Config:
    """配置管理类"""
    
    DEFAULT_CONFIG = {
        "api": {
            "job_url": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
            "token": "YOUR_AISTUDIO_TOKEN",
            "model": "PaddleOCR-VL-1.6"
        },
        "options": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_chart_recognition": False
        },
        "output": {
            "dir": "output",
            "download_md_images": True,
            "download_output_images": True
        },
        "polling": {
            "interval": 5,
            "timeout": 0
        },
        "batch": {
            "recursive": False,
            "extensions": [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]
        }
    }
    
    def __init__(self, config_path: Optional[str] = None):
        """初始化配置"""
        self.config = self.DEFAULT_CONFIG.copy()

        # 加载配置文件
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                file_config = yaml.safe_load(f)
                self._merge_config(file_config)

        # 环境变量注入 token (优先于配置文件), 避免 token 进入版本库
        env_token = os.environ.get("PADDLEOCR_API_TOKEN")
        if env_token:
            self.config["api"]["token"] = env_token
    
    def _merge_config(self, file_config: Dict[str, Any]):
        """合并配置文件"""
        for section, values in file_config.items():
            if section in self.config and isinstance(values, dict):
                self.config[section].update(values)
            else:
                self.config[section] = values
    
    def update_from_args(self, args: argparse.Namespace):
        """从命令行参数更新配置"""
        if args.token:
            self.config["api"]["token"] = args.token
        if args.model:
            self.config["api"]["model"] = args.model
        if args.output:
            self.config["output"]["dir"] = args.output
        if args.interval:
            self.config["polling"]["interval"] = args.interval
        if args.timeout:
            self.config["polling"]["timeout"] = args.timeout
        if args.recursive:
            self.config["batch"]["recursive"] = True
        
        # 识别选项
        if args.orientation:
            self.config["options"]["use_doc_orientation_classify"] = True
        if args.unwarping:
            self.config["options"]["use_doc_unwarping"] = True
        if args.chart:
            self.config["options"]["use_chart_recognition"] = True
    
    def get(self, section: str, key: str = None) -> Any:
        """获取配置值"""
        if key is None:
            return self.config.get(section, {})
        return self.config.get(section, {}).get(key)


class PaddleOCRClient:
    """PaddleOCR API 客户端"""
    
    def __init__(self, config: Config):
        self.config = config
        self.job_url = config.get("api", "job_url")
        self.token = config.get("api", "token")
        self.model = config.get("api", "model")
        self.headers = {
            "Authorization": f"bearer {self.token}"
        }
    
    def submit_job(self, file_path: str) -> str:
        """提交 OCR 任务"""
        print(f"📄 处理文件: {file_path}")
        
        optional_payload = {
            "useDocOrientationClassify": self.config.get("options", "use_doc_orientation_classify"),
            "useDocUnwarping": self.config.get("options", "use_doc_unwarping"),
            "useChartRecognition": self.config.get("options", "use_chart_recognition")
        }
        
        if file_path.startswith("http"):
            # URL 模式
            self.headers["Content-Type"] = "application/json"
            payload = {
                "fileUrl": file_path,
                "model": self.model,
                "optionalPayload": optional_payload
            }
            response = requests.post(self.job_url, json=payload, headers=self.headers)
        else:
            # 本地文件模式
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"文件不存在: {file_path}")
            
            data = {
                "model": self.model,
                "optionalPayload": json.dumps(optional_payload)
            }
            
            with open(file_path, "rb") as f:
                files = {"file": f}
                response = requests.post(self.job_url, headers=self.headers, data=data, files=files)
        
        if response.status_code != 200:
            raise Exception(f"API 请求失败 [{response.status_code}]: {response.text}")
        
        job_id = response.json()["data"]["jobId"]
        print(f"✅ 任务提交成功，Job ID: {job_id}")
        return job_id
    
    def poll_job(self, job_id: str) -> Dict[str, Any]:
        """轮询任务状态"""
        print("⏳ 开始轮询任务结果...")
        
        interval = self.config.get("polling", "interval")
        timeout = self.config.get("polling", "timeout")
        start_time = time.time()
        
        while True:
            # 检查超时
            if timeout > 0 and (time.time() - start_time) > timeout:
                raise TimeoutError(f"任务超时 (等待超过 {timeout} 秒)")
            
            response = requests.get(f"{self.job_url}/{job_id}", headers=self.headers)
            
            if response.status_code != 200:
                raise Exception(f"查询任务失败 [{response.status_code}]: {response.text}")
            
            data = response.json()["data"]
            state = data["state"]
            
            if state == "pending":
                print("  ⏸️  状态: 等待中...")
            elif state == "running":
                try:
                    progress = data["extractProgress"]
                    total = progress["totalPages"]
                    extracted = progress["extractedPages"]
                    print(f"  🔄 状态: 处理中 ({extracted}/{total} 页)")
                except KeyError:
                    print("  🔄 状态: 处理中...")
            elif state == "done":
                progress = data["extractProgress"]
                extracted = progress["extractedPages"]
                start = progress["startTime"]
                end = progress["endTime"]
                print(f"✅ 任务完成！成功处理 {extracted} 页")
                print(f"   开始时间: {start}")
                print(f"   结束时间: {end}")
                return data
            elif state == "failed":
                error_msg = data.get("errorMsg", "未知错误")
                raise Exception(f"任务失败: {error_msg}")
            
            time.sleep(interval)


class ResultProcessor:
    """结果处理器"""
    
    def __init__(self, config: Config):
        self.config = config
        self.output_dir = config.get("output", "dir")
        self.download_md_images = config.get("output", "download_md_images")
        self.download_output_images = config.get("output", "download_output_images")
    
    def process(self, job_data: Dict[str, Any], source_name: str):
        """处理任务结果"""
        jsonl_url = job_data.get("resultUrl", {}).get("jsonUrl")
        
        if not jsonl_url:
            raise Exception("未找到结果 URL")
        
        # 下载 JSONL 结果
        print("📥 下载识别结果...")
        response = requests.get(jsonl_url)
        response.raise_for_status()
        
        # 创建输出目录
        output_subdir = os.path.join(self.output_dir, source_name)
        os.makedirs(output_subdir, exist_ok=True)
        
        # 解析并保存结果
        lines = response.text.strip().split('\n')
        page_num = 0
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            result = json.loads(line)["result"]
            
            for res in result.get("layoutParsingResults", []):
                # 保存 Markdown
                md_filename = os.path.join(output_subdir, f"page_{page_num:03d}.md")
                with open(md_filename, "w", encoding="utf-8") as f:
                    f.write(res["markdown"]["text"])
                print(f"  💾 保存 Markdown: {md_filename}")
                
                # 下载 Markdown 中的图片
                if self.download_md_images:
                    for img_path, img_url in res["markdown"].get("images", {}).items():
                        full_img_path = os.path.join(output_subdir, img_path)
                        os.makedirs(os.path.dirname(full_img_path), exist_ok=True)
                        img_bytes = requests.get(img_url).content
                        with open(full_img_path, "wb") as f:
                            f.write(img_bytes)
                        print(f"  🖼️  保存图片: {full_img_path}")
                
                # 下载输出图片
                if self.download_output_images:
                    for img_name, img_url in res.get("outputImages", {}).items():
                        img_response = requests.get(img_url)
                        if img_response.status_code == 200:
                            filename = os.path.join(output_subdir, f"{img_name}_{page_num:03d}.jpg")
                            with open(filename, "wb") as f:
                                f.write(img_response.content)
                            print(f"  🖼️  保存输出图片: {filename}")
                
                page_num += 1
        
        print(f"✅ 完成！共处理 {page_num} 页，结果保存在: {output_subdir}")


class PPOCR:
    """P-POCR 主控制器"""
    
    def __init__(self, config: Config):
        self.config = config
        self.client = PaddleOCRClient(config)
        self.processor = ResultProcessor(config)
    
    def process_file(self, file_path: str):
        """处理单个文件"""
        try:
            # 提交任务
            job_id = self.client.submit_job(file_path)
            
            # 轮询结果
            job_data = self.client.poll_job(job_id)
            
            # 处理结果
            source_name = Path(file_path).stem if not file_path.startswith("http") else "url_result"
            self.processor.process(job_data, source_name)
            
        except Exception as e:
            print(f"❌ 处理失败: {e}")
            raise
    
    def process_batch(self, input_path: str):
        """批量处理文件"""
        input_path = Path(input_path)
        
        if input_path.is_file():
            # 单个文件
            self.process_file(str(input_path))
        elif input_path.is_dir():
            # 目录批处理
            recursive = self.config.get("batch", "recursive")
            extensions = self.config.get("batch", "extensions")
            
            # 收集文件
            files = []
            if recursive:
                for ext in extensions:
                    files.extend(input_path.rglob(f"*{ext}"))
            else:
                for ext in extensions:
                    files.extend(input_path.glob(f"*{ext}"))
            
            if not files:
                print(f"⚠️  未找到支持的文件 (扩展名: {', '.join(extensions)})")
                return
            
            print(f"📚 找到 {len(files)} 个文件待处理")
            
            # 逐个处理
            success_count = 0
            for i, file_path in enumerate(files, 1):
                print(f"\n[{i}/{len(files)}] ", end="")
                try:
                    self.process_file(str(file_path))
                    success_count += 1
                except Exception as e:
                    print(f"跳过此文件: {e}")
                    continue
            
            print(f"\n🎉 批处理完成！成功: {success_count}/{len(files)}")
        else:
            raise FileNotFoundError(f"路径不存在: {input_path}")


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="P-POCR: PaddleOCR-VL PDF → Markdown 转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 处理单个 PDF
  python pocr.py document.pdf
  
  # 批量处理目录
  python pocr.py ./documents/
  
  # 使用自定义配置
  python pocr.py document.pdf --config my_config.yaml
  
  # 指定输出目录
  python pocr.py document.pdf --output ./results
  
  # 启用高级识别选项
  python pocr.py document.pdf --orientation --unwarping --chart
  
  # 处理 URL
  python pocr.py https://example.com/document.pdf
        """
    )
    
    parser.add_argument(
        "input",
        help="输入文件路径、目录或 URL"
    )
    
    parser.add_argument(
        "--config", "-c",
        help="配置文件路径 (默认: config.yaml)"
    )
    
    parser.add_argument(
        "--output", "-o",
        help="输出目录 (默认: output)"
    )
    
    parser.add_argument(
        "--token", "-t",
        help="API Token"
    )
    
    parser.add_argument(
        "--model", "-m",
        help="识别模型 (默认: PaddleOCR-VL-1.6)"
    )
    
    parser.add_argument(
        "--interval", "-i",
        type=int,
        help="轮询间隔秒数 (默认: 5)"
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        help="最大等待时间秒数 (默认: 0 无限等待)"
    )
    
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="递归扫描子目录"
    )
    
    # 高级识别选项
    parser.add_argument(
        "--orientation",
        action="store_true",
        help="启用文档方向分类"
    )
    
    parser.add_argument(
        "--unwarping",
        action="store_true",
        help="启用文档矫正"
    )
    
    parser.add_argument(
        "--chart",
        action="store_true",
        help="启用图表识别"
    )
    
    return parser.parse_args()


def main():
    """主入口"""
    print("=" * 60)
    print("  P-POCR: PaddleOCR-VL PDF → Markdown 转换工具")
    print("=" * 60)
    
    # 解析参数
    args = parse_args()
    
    # 加载配置
    config_path = args.config or "config.yaml"
    config = Config(config_path if os.path.exists(config_path) else None)
    config.update_from_args(args)
    
    # 创建控制器并处理
    pocr = PPOCR(config)
    
    try:
        pocr.process_batch(args.input)
        print("\n✨ 所有任务完成！")
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
