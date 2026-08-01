# NIR Quantification

面向 `14` 类纤维、`1-4` 组分、`900-1700nm / 228点` 的近红外混纺定量离线流水线。

## 功能

- 批量解析设备导出的原始 CSV
- 输出 `manifest`、`rejection report`、`audit report` 和 `group split`
- 使用 `1D Inception` 双头模型做存在性预测和配比回归
- 对单个原始 CSV 做推理，输出最多 4 个纤维及其百分比
- 提供本地 Web 光谱管理工具，支持导入、分类浏览、剔除、撤销和原文保真导出

## 安装

仅使用数据构建功能时不需要额外依赖。训练和推理需要安装 `torch`：

```bash
pip install -e '.[train]'
```

光谱管理 Web 工具需要：

```bash
pip install -e '.[web]'
```

前端开发需要在 `frontend/` 下安装 Node 依赖：

```bash
cd frontend
npm install
```

## 命令

构建 manifest、审计报告和 split：

```bash
nirq build-manifest \
  --input-dir /path/to/raw_csvs \
  --manifest-out outputs/manifest.jsonl \
  --rejections-out outputs/rejections.jsonl \
  --audit-out outputs/audit.json \
  --splits-out outputs/splits.json
```

训练：

```bash
nirq train \
  --manifest outputs/manifest.jsonl \
  --splits outputs/splits.json \
  --output-dir outputs/run_001
```

单文件推理：

```bash
nirq predict \
  --csv /path/to/sample.csv \
  --bundle outputs/run_001/model_bundle.pt
```

启动管理工具后端：

```bash
nirq web --port 8000
```

默认只监听本机 `127.0.0.1`。如需对局域网开放，请使用带身份认证的反向代理，不要直接暴露管理端口。

前端开发：

```bash
cd frontend
npm run dev
```

Docker 启动：

```bash
docker compose up -d --build
```

启动后浏览器访问 [http://localhost:8000](http://localhost:8000)。

生产环境可使用保留 `8001` 端口和 CIFS 导入卷的配置：

```bash
docker compose -f docker-compose.production.yml up -d --build
```

容器入口会先把 `/data` 和导出目录修正为运行 UID/GID（默认 `10001:10001`），然后立即降权启动服务。这样可以兼容历史上由 root 容器创建的 SQLite、WAL 和导出目录；升级前仍建议备份 `data/`。如部署环境需要指定其他 UID/GID，可设置 `NIRQ_RUN_UID` 和 `NIRQ_RUN_GID`。

如果确认旧数据库没有需要保留的数据，可以直接停止服务并删除 SQLite 文件后重建：

```bash
docker compose -f docker-compose.production.yml down
sudo rm -f ./data/spectra.sqlite3 ./data/spectra.sqlite3-wal ./data/spectra.sqlite3-shm
docker compose -f docker-compose.production.yml up -d --build
```
