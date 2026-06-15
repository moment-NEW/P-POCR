# P-POCR: PaddleOCR-VL PDF → Markdown 转换工具

一个开箱即用的 PDF 文档 OCR 转换工具，基于 PaddleOCR-VL API，将 PDF/图片文档转换为 AI 可读的 Markdown 格式。

## ✨ 特性

- 🚀 **开箱即用** - 安装依赖后即可运行，无需复杂配置
- 📄 **多格式支持** - 支持 PDF、PNG、JPG、TIFF、BMP 等格式
- 🖼️ **智能识别** - 支持文档方向分类、文档矫正、图表识别
- 📚 **批量处理** - 支持单文件、目录批处理、递归扫描
- ⚙️ **灵活配置** - YAML 配置文件 + 命令行参数，支持高级定制
- 🎯 **URL 支持** - 直接处理网络文件 URL
- 📊 **进度显示** - 实时显示处理进度和状态

## 📦 安装

### 1. 克隆或下载项目

```bash
cd P-POCR
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

依赖包：
- `requests` >= 2.28.0
- `PyYAML` >= 6.0

## 🚀 快速开始

### 处理单个 PDF

```bash
python pocr.py document.pdf
```

### 批量处理目录

```bash
python pocr.py ./documents/
```

### 处理网络文件

```bash
python pocr.py https://example.com/document.pdf
```

## 📖 使用方法

### 基本用法

```bash
python pocr.py <输入路径> [选项]
```

**输入路径** 可以是：
- 单个文件路径（PDF、图片等）
- 目录路径（批量处理）
- 文件 URL

### 命令行选项

| 选项 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--config` | `-c` | 配置文件路径 | `config.yaml` |
| `--output` | `-o` | 输出目录 | `output` |
| `--token` | `-t` | API Token | 配置文件中的值 |
| `--model` | `-m` | 识别模型 | `PaddleOCR-VL-1.6` |
| `--interval` | `-i` | 轮询间隔（秒） | `5` |
| `--timeout` | - | 最大等待时间（秒） | `0`（无限） |
| `--recursive` | `-r` | 递归扫描子目录 | `false` |
| `--orientation` | - | 启用文档方向分类 | `false` |
| `--unwarping` | - | 启用文档矫正 | `false` |
| `--chart` | - | 启用图表识别 | `false` |

### 使用示例

#### 1. 指定输出目录

```bash
python pocr.py document.pdf --output ./results
```

#### 2. 启用高级识别功能

```bash
python pocr.py document.pdf --orientation --unwarping --chart
```

#### 3. 递归处理子目录

```bash
python pocr.py ./documents/ --recursive
```

#### 4. 使用自定义配置

```bash
python pocr.py document.pdf --config my_config.yaml
```

#### 5. 设置超时时间

```bash
python pocr.py document.pdf --timeout 300
```

## ⚙️ 配置文件

项目根目录下的 `config.yaml` 包含所有可配置选项：

```yaml
# API 设置
api:
  job_url: "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
  token: "your-token-here"
  model: "PaddleOCR-VL-1.6"

# 识别选项
options:
  use_doc_orientation_classify: false
  use_doc_unwarping: false
  use_chart_recognition: false

# 输出设置
output:
  dir: "output"
  download_md_images: true
  download_output_images: true

# 轮询设置
polling:
  interval: 5
  timeout: 0

# 批处理设置
batch:
  recursive: false
  extensions:
    - ".pdf"
    - ".png"
    - ".jpg"
    - ".jpeg"
    - ".tiff"
    - ".bmp"
```

### 配置优先级

**命令行参数** > **配置文件** > **内置默认值**

### 自定义配置

1. 复制默认配置：
   ```bash
   cp config.yaml my_config.yaml
   ```

2. 编辑 `my_config.yaml`

3. 使用自定义配置：
   ```bash
   python pocr.py document.pdf --config my_config.yaml
   ```

## 📁 输出结构

处理结果保存在输出目录下，每个源文件创建一个子目录：

```
output/
├── document/
│   ├── page_000.md          # 第 1 页 Markdown
│   ├── page_001.md          # 第 2 页 Markdown
│   ├── page_002.md          # 第 3 页 Markdown
│   ├── images/              # Markdown 引用的图片
│   │   ├── fig1.png
│   │   └── fig2.png
│   └── page_image_000.jpg   # 页面渲染图
└── another_doc/
    ├── page_000.md
    └── ...
```

## 🔧 高级用法

### 作为 Python 模块使用

```python
from pocr import Config, PPOCR

# 加载配置
config = Config("config.yaml")

# 创建处理器
pocr = PPOCR(config)

# 处理单个文件
pocr.process_file("document.pdf")

# 批量处理
pocr.process_batch("./documents/")
```

### 自定义配置

```python
from pocr import Config, PPOCR

# 创建配置
config = Config()

# 动态修改配置
config.config["api"]["token"] = "your-new-token"
config.config["options"]["use_chart_recognition"] = True
config.config["output"]["dir"] = "custom_output"

# 使用配置
pocr = PPOCR(config)
pocr.process_file("document.pdf")
```

### 批处理脚本示例

```python
from pocr import Config, PPOCR
from pathlib import Path

config = Config("config.yaml")
pocr = PPOCR(config)

# 处理所有 PDF
pdf_dir = Path("./pdfs")
for pdf in pdf_dir.glob("*.pdf"):
    try:
        pocr.process_file(str(pdf))
    except Exception as e:
        print(f"处理 {pdf} 失败: {e}")
```

## 🛠️ 故障排除

### 1. API Token 无效

**错误**: `API 请求失败 [401]`

**解决**: 检查并更新 Token
```bash
python pocr.py document.pdf --token your-valid-token
```

或在 `config.yaml` 中更新：
```yaml
api:
  token: "your-valid-token"
```

### 2. 文件不存在

**错误**: `文件不存在: /path/to/file.pdf`

**解决**: 检查文件路径是否正确

### 3. 任务超时

**错误**: `任务超时 (等待超过 300 秒)`

**解决**: 增加超时时间或检查网络连接
```bash
python pocr.py document.pdf --timeout 600
```

### 4. 依赖缺失

**错误**: `请先安装 requests 库`

**解决**: 安装依赖
```bash
pip install -r requirements.txt
```

## 📝 注意事项

1. **API 限制**: 注意 PaddleOCR API 的调用频率和配额限制
2. **文件大小**: 大文件处理时间较长，建议设置合理的超时时间
3. **网络稳定**: 确保网络连接稳定，避免中断
4. **存储空间**: 批量处理时确保有足够的存储空间

## 🔗 相关链接

- [PaddleOCR 官方文档](https://github.com/PaddlePaddle/PaddleOCR)
- [PaddleOCR API 平台](https://aistudio.baidu.com/aistudio/projectdetail/5234391)

## 📄 许可证

本项目仅供学习和研究使用。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**提示**: 首次使用前，请确保已获取有效的 PaddleOCR API Token。
