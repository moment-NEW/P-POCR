# P-POCR 项目结构

```
P-POCR/
├── pocr.py              # PDF/图片 OCR 主程序入口 (CLI)
├── video_ocr.py         # 视频字幕 OCR (ffmpeg 抽帧 → PaddleOCR → SRT)
├── config.yaml          # 默认配置文件
├── requirements.txt     # Python 依赖
├── README.md           # 使用文档
├── Videos/             # 建议存放待处理视频
└── output/             # 输出目录 (运行时创建)
    ├── <文件名>/        # PDF/图片结果
    │   ├── page_000.md
    │   ├── page_001.md
    │   └── images/
    └── <视频名>/        # 视频结果
        ├── frames/      # 裁切后的帧 (缓存)
        ├── ocr_cache.json
        ├── <视频名>.srt
        └── <视频名>.txt
```

## 文件说明

### 核心文件

- **pocr.py** - PDF/图片 OCR 主程序
  - `Config` 类：配置管理
  - `PaddleOCRClient` 类：API 客户端
  - `ResultProcessor` 类：结果处理
  - `PPOCR` 类：主控制器

- **video_ocr.py** - 视频字幕 OCR 工具 (依赖 ffmpeg)
  - `extract_frames`：ffmpeg 抽帧 + 裁切底部 + mpdecimate 去重
  - `ocr_file` / `ocr_frames`：PaddleOCR API 并发识别 + 本地缓存
  - `merge_runs` / `write_outputs`：按时间轴合并生成 SRT/TXT

- **config.yaml** - YAML 配置文件
  - API 设置（URL、Token、模型）
  - 识别选项（方向分类、矫正、图表识别）
  - 输出设置（目录、图片下载）
  - 轮询设置（间隔、超时）
  - 批处理设置（递归、扩展名）
  - 视频设置（抽帧率、裁切比例、并发、去重）

- **requirements.txt** - Python 依赖包列表

### 输出文件

处理结果按源文件名组织：
- `page_XXX.md` - 每页的 Markdown 文本
- `images/` - Markdown 中引用的图片
- `*_XXX.jpg` - 页面渲染图（可选）
- `<视频名>.srt` / `<视频名>.txt` - 视频字幕
