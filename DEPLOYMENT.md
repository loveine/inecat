# Rocky Linux 9.4 部署与运维手册

本组件按“保留原静态站点、并行增加社区路由”设计。现有站点目录、图片/视频文件和证书都不需要迁移。部署前必须在服务器上核对真实 Nginx 配置、站点根目录、域名证书、剩余空间和 SELinux 状态；当前交付环境没有这些服务器文件。

现有静态照片继续从旧页面和 URL 公开访问，不会自动显示在社区的新画廊列表里。部署后可以在现有首页或导航中加入 `/community/` 链接；如果你希望社区画廊也列出旧照片，需要先确认实际只读图片目录，再单独设计受控导入清单。

## 0. 先做部署前确认

在服务器上只读核对当前服务和路径：

```bash
sudo nginx -T
sudo systemctl status nginx --no-pager
sudo df -h
sudo getenforce
sudo ls -ld /usr/share/nginx/html /var/log/nginx
```

确认 `inecat.online` 的 HTTPS `server` 块、实际静态根目录、include 关系，以及现有图片/视频的 URL。不要用新配置覆盖原 server 块。记录当前配置文件清单和静态目录路径。确认可恢复的服务器快照或独立备份后再继续。

建议新服务目录使用 `/srv/inecat-gallery`，上传目录使用 `/srv/inecat-gallery/data/uploads`。它与现有站点根目录分开。部署用户需要 Docker Compose、sudo、Nginx 配置和备份权限。首次管理员命令会要求在终端输入密码。

如果 `getenforce` 显示 `Enforcing`，不要关闭 SELinux。`:Z` 会给容器 bind mount 设置私有容器标签；上线前需要确认宿主 Nginx 对上传目录具有只读访问权限。若 Nginx 返回 403 且文件 Unix 权限正确，先检查 `sudo ausearch -m AVC -ts recent` 中的拒绝记录，再按 Rocky/RHEL 的 SELinux 策略配置专用上下文和最小权限规则。不要直接运行 `setenforce 0`，也不要盲目 relabel 整个网站目录。

## 1. 准备主机和密钥

安装并启用 Docker Engine/Compose 插件（按组织认可的软件源和版本策略安装）。不要把 MySQL 端口暴露给公网。将交付目录复制到 `/srv/inecat-gallery`，并建立仅管理员可读的环境文件：

```bash
sudo install -d -m 0750 /srv/inecat-gallery
sudo install -d -o 10001 -g 10001 -m 0755 /srv/inecat-gallery/data/uploads
cd /srv/inecat-gallery
sudo cp .env.example .env
sudo chmod 0600 .env
openssl rand -hex 32
```

把 `openssl rand -hex 32` 生成的三组不同随机值分别用于 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、`SESSION_SECRET` 和 `CSRF_SECRET`（共四个值）。将数据库密码原样写入 `MYSQL_PASSWORD`，并在 `DATABASE_URL` 中同步填写；十六进制密码无需 URL 编码。`.env` 只保存在服务器，不要上传到代码仓库、聊天或工单。`COOKIE_SECURE=true` 必须保留在生产 HTTPS 环境。

检查上传目录属主为容器进程 UID/GID 10001；SELinux 开启时 Compose bind mount 使用 `:Z` 私有标签。静态 Nginx 进程需要遍历并读取该目录，因此目录为 0755、图片为 0644；不要给上传目录设置可执行权限。若部署路径或 UID 改动，先确认 Nginx 和容器两端的读写权限。

## 2. 先单独启动并建表

```bash
cd /srv/inecat-gallery
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=100 api db
sudo docker compose exec api alembic upgrade head
sudo docker compose exec api python -m app.cli create-admin
```

命令作用：构建并启动 API 与 MySQL；检查容器状态和启动日志；应用可追踪的数据库迁移；最后交互创建首个管理员。API 仅发布到 `127.0.0.1:18080`，数据库只在 Docker 私有网络中，不应增加公网端口。管理员密码不回显、不进 shell 历史，随后保存到密码管理器。迁移命令重复执行可安全检查版本；管理员创建命令在已有管理员时会拒绝再次创建。

本机检查健康端点：

```bash
curl --fail http://127.0.0.1:18080/api/health
```

此时还没有改动 Nginx，也还没有公开社区路由。浏览器 cookie 的 `Secure` 标记要求 HTTPS；不要为了图方便在公网改为 `false`。仅限隔离的 localhost 测试可临时关闭，测试后恢复 `true` 并重建/重启 API。

## 3. 备份现有站点并添加 Nginx 路由

**先备份，再编辑。** 将下面的备份文件名加上日期并存放在有权限控制的备份盘；也应有服务器快照或另一份独立副本：

```bash
sudo cp -a /etc/nginx /etc/nginx.backup-$(date +%F-%H%M%S)
sudo tar -C /usr/share/nginx -czf /root/inecat-static-backup-$(date +%F-%H%M%S).tar.gz html
```

上例静态路径只是从之前对话得知的假设值；必须先用 `nginx -T` 确认真实 `root`，再替换 tar 命令的目录。备份后把以下速率限制区加在 Nginx `http {}` 中一次；名称如已存在则复用或改名：

```nginx
limit_req_zone $binary_remote_addr zone=inecat_auth:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=inecat_write:10m rate=30r/m;
limit_conn_zone $binary_remote_addr zone=inecat_upload:10m;
```

把以下 `location` 合并到现有 `inecat.online` 的 HTTPS `server {}`。保留已有 `root`、TLS、静态文件、缓存、视频 Range 处理和其他 location。只增加路由，不替换整个配置：

```nginx
location = /community {
    return 308 /community/;
}

location ^~ /community/ {
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_connect_timeout 5s;
    proxy_read_timeout 30s;
}

location ^~ /api/ {
    client_max_body_size 12m;
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_connect_timeout 5s;
    proxy_read_timeout 60s;
    limit_req zone=inecat_write burst=20 nodelay;
    limit_conn inecat_upload 2;
}

location = /api/login {
    client_max_body_size 16k;
    proxy_pass http://127.0.0.1:18080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    limit_req zone=inecat_auth burst=5 nodelay;
}

location = /api/register {
    client_max_body_size 16k;
    proxy_pass http://127.0.0.1:18080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    limit_req zone=inecat_auth burst=5 nodelay;
}

location ^~ /user-uploads/ {
    alias /srv/inecat-gallery/data/uploads/;
    autoindex off;
    types { image/jpeg jpg; image/png png; image/webp webp; }
    default_type application/octet-stream;
    add_header X-Content-Type-Options nosniff always;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}
```

因为 `/api/` 通用规则也匹配登录/注册，上面两个精确匹配 location 会优先用于认证限流。注意 `limit_req_zone` 必须放在 `http {}`，不能放进 `server {}`。如果已经有更严格的 `client_max_body_size`，只为 `/api/` 调整，不扩大整个静态站点的限制。`user-uploads` 路径映射到独立目录，Nginx 只读；应用输出 UUID 文件名，禁止目录列表。

保存后先检查语法，再平滑重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

如果 `nginx -t` 失败，不要 reload；修正刚加入的片段，原配置备份可用于回滚。

## 4. 上线后核对

1. 打开 `https://inecat.online/community/`，再打开原首页、原图片和视频 URL，确认旧内容仍正常。
2. 退出状态下确认公开画廊可浏览；注册一个普通账号，登录并上传一张测试图。
3. 用另一个普通账号尝试编辑/删除第一账号照片；API 应拒绝，照片仍存在。直接猜测/更改照片 ID 也必须无法跨账号操作。
4. 管理员检查用户列表、停用/恢复一个测试用户，并确认停用后旧会话失效。
5. 用无效文件、超限图片和未登录上传检查被拒绝；观察浏览器与 Nginx 日志。
6. 检查 `sudo docker compose ps`、`sudo docker compose logs --tail=100 api db`、`sudo df -h` 和 `/var/log/nginx/`。设置磁盘告警并定期查看容器日志。

以上是上线验收步骤，部署者执行后逐项记录结果。本交付没有连接线上网站，也没有替你执行这些验证。

## 5. 备份与恢复

建议每日备份 MySQL 与上传目录到服务器之外的加密备份位置，并定期做恢复演练。照片文件和数据库记录需来自同一时间点或短时间窗口；备份期间可短暂停止 API 写入：

```bash
cd /srv/inecat-gallery
sudo docker compose stop api
sudo install -d -m 0700 /secure-backup/inecat-gallery
sudo bash <<'BACKUP'
set -euo pipefail
umask 077
cd /srv/inecat-gallery
trap 'docker compose start api' EXIT
stamp=$(date +%F)
docker compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -u root --single-transaction "$MYSQL_DATABASE"' | gzip > "/secure-backup/inecat-gallery/db-${stamp}.sql.gz"
tar -C /srv/inecat-gallery/data -czf "/secure-backup/inecat-gallery/uploads-${stamp}.tar.gz" uploads
BACKUP
```

先将 `/secure-backup` 替换为实际的加密备份挂载点，并确认容量、权限、校验和以及备份可读。`.env`、Nginx 配置和静态站点也需要独立备份；密钥应放在受控密码库/密钥管理系统，不应与公开代码一起保存。

恢复时先停止 API，在隔离环境或明确维护窗口中恢复 MySQL 与 `uploads`，确认属主 UID/GID 10001、SELinux 标签和磁盘空间，再启动 API 与 Nginx。不要把生产数据库恢复到正在使用的数据库上而不先做当前快照。数据库密码未变时使用：

```bash
gunzip -c /secure-backup/inecat-gallery-db-YYYY-MM-DD.sql.gz | sudo docker compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root "$MYSQL_DATABASE"'
sudo tar -C /srv/inecat-gallery/data -xzf /secure-backup/inecat-gallery-uploads-YYYY-MM-DD.tar.gz
sudo chown -R 10001:10001 /srv/inecat-gallery/data/uploads
sudo docker compose start api
```

实际恢复前先确认备份对应数据库/上传目录，并按当时 MySQL 认证配置调整；不要把密码写进命令参数或 shell 历史。

## 6. 回滚

若新增路由造成故障：恢复备份的 Nginx 配置，运行 `sudo nginx -t` 成功后 reload。旧静态站点一直由既有 `root` 提供，回滚 Nginx 路由即可恢复原访问路径。社区服务可停止但先不要删除数据卷：

```bash
cd /srv/inecat-gallery
sudo docker compose stop api
```

不要运行 `docker compose down -v`，它会删除 MySQL 数据卷。修复期间保留备份、上传目录和数据库卷；彻底移除服务属于单独的数据删除操作，需要先完成数据导出和备份验证。

## 已知运维边界

- 当前使用 `mysql:8.4` Docker 镜像标签及固定 Python 依赖版本（依赖版本按 2026-11-12 的 PyPI 信息核对）；上线前仍要按组织漏洞扫描与升级流程复核镜像和依赖，再构建部署。
- 图片上传接受单张 10 MiB 内、最多 1200 万像素的 JPEG、PNG、WebP。每个 API 进程一次解码一张图，Nginx 限制同一来源并发 API 连接，容器 `/tmp` 限制为 128 MiB。视频继续使用现有静态站点文件；新社区不接收视频。
- Nginx 速率限制按远端 IP 生效。如果站点在 CDN/代理后，需先正确配置可信真实 IP，避免所有请求共用代理 IP 或伪造来源。
- 数据表通过 Alembic 管理；首次安装也必须在创建首个管理员前运行 `alembic upgrade head`。部署新版本前先备份数据库；不要运行 `alembic downgrade`，当前迁移会拒绝可能删除用户与照片元数据的降级操作，应通过备份恢复处理回滚。
- 组件尚未实现邮件找回、邮箱验证、审核队列和多节点限流。这些不影响当前的所有权隔离，但属于后续运营能力。
