# P-POCR 项目结构

```
P-POCR/
├── pocr.py              # 主程序入口 (CLI)
├── config.yaml          # 默认配置文件
├── requirements.txt     # Python 依赖
├── README.md           # 使用文档
└── output/             # 输出目录 (运行时创建)
    └── <文件名>/
        ├── page_000.md
        ├── page_001.md
        └── images/
```

## 文件说明

### 核心文件

- **pocr.py** - 主程序，包含所有功能逻辑
  - `Config` 类：配置管理
  - `PaddleOCRClient` 类：API 客户端
  - `ResultProcessor` 类：结果处理
  - `PPOCR` 类：主控制器

- **config.yaml** - YAML 配置文件
  - API 设置（URL、Token、模型）
  - 识别选项（方向分类、矫正、图表识别）
  - 输出设置（目录、图片下载）
  - 轮询设置（间隔、超时）
  - 批处理设置（递归、扩展名）

- **requirements.txt** - Python 依赖包列表

### 输出文件

处理结果按源文件名组织：
- `page_XXX.md` - 每页的 Markdown 文本
- `images/` - Markdown 中引用的图片
- `*_XXX.jpg` - 页面渲染图（可选）
