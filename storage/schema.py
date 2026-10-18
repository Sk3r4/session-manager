"""schema 版本化迁移:PRAGMA user_version 驱动,一版一函数,幂等。

版本史:
  v1 (2026-10-15): 瘦身后基线。v0 = 无版本号的存量库(含瘦身前形状)或全新库:
      建表补齐(IF NOT EXISTS 全量 DDL)+ 极旧库补列(2026-09 时代 guarded ALTER
      原样搬移)+ 瘦身死列清理(sessions.status/raw_meta/pinned/pinned_at、
      projects.status,幂等 DROP)。
  此后 schema 变更一律: 新增 _migrate_N_to_M + SCHEMA_VERSION 递增,
  不再用 try/except ALTER 自愈补丁。
"""
import sqlite3

SCHEMA_VERSION = 1


def _migrate_0_to_1(conn):
    """v0(存量/全新库) → v1(瘦身基线):建表 + 补列 + 删死列,全部幂等。"""
    # WAL 模式:多写者共存(Web 服务 / MCP 进程 / AI 标注后台线程)下避免整库锁。
    # 持久化属性,设置一次长期有效;只读连接(mode=ro)在 WAL 下本机并发读安全。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT,
            summary TEXT,
            project_dir TEXT,
            created_at REAL,
            last_active_at REAL,
            source_path TEXT,
            deleted INTEGER DEFAULT 0,
            title_source TEXT
        )
    """)
    # 兼容旧表
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN deleted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 标题来源标记：NULL=自动（扫描可刷新），'manual'=用户手动重命名（扫描永不覆盖）
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN title_source TEXT")
    except sqlite3.OperationalError:
        pass
    # CLI 标题基线:跟随"最近一次扫描所见"的 CLI 侧标题(含 AI 漂移)。
    # 配合 title_author 判定"CLI 侧人工改名":新标题 ≠ 基线 且作者为 human 才覆盖 manual。
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN cli_title TEXT")
    except sqlite3.OperationalError:
        pass
    # 标题作者:'human' = CLI 侧人工命名(可覆盖 manual);NULL = AI 生成/派生。
    # AI 标题随会话推进持续漂移,漂移不构成"CLI 改过名"——否则 web 手动改名
    # 会在下一次扫描被打回(2026-10 修复的回退 bug)。
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN title_author TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT DEFAULT CURRENT_TIMESTAMP,
            provider_id TEXT,
            sessions_found INTEGER,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            path TEXT,
            created_at REAL,
            last_active_at REAL,
            session_count INTEGER DEFAULT 0,
            archived_count INTEGER DEFAULT 0,
            display_name TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_path ON projects(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON sessions(provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_active ON sessions(last_active_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted ON sessions(deleted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_dir ON sessions(project_dir)")
    # 合集(用户自定义分类,不映射真实会话数据——纯索引层)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at REAL,
            sort_order REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_collections (
            session_id TEXT NOT NULL,
            collection_id INTEGER NOT NULL,
            added_at REAL,
            PRIMARY KEY (session_id, collection_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_collection ON session_collections(collection_id)")
    # 标签(自由文本多对多,纯索引层——与合集同构,但标签即其名,无实体表)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            added_at REAL,
            PRIMARY KEY (session_id, tag)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_st_tag ON session_tags(tag)")
    # 内容全文索引(FTS5 trigram:中文任意 ≥3 字符子串可查;短词 LIKE 兜底)
    # 只索引对话文本(text/thinking),工具输出不进索引;fts_state 记基线做增量
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
            content, sid UNINDEXED, tokenize='trigram'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fts_state (
            session_id TEXT PRIMARY KEY,
            last_active_at REAL
        )
    """)
    # 合集动态模式(2026-09-21 追踪线收敛):query=关键词(成员=自动匹配∪手动),
    # watched=追踪态(侧栏面板/列表色线高亮/进度注记的总开关)
    try:
        conn.execute("ALTER TABLE collections ADD COLUMN query TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE collections ADD COLUMN watched INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 旧 tracked_threads(追踪线)并入合集:动态合集(name=query)且默认追踪;随后移除旧表
    try:
        legacy = conn.execute("SELECT name, created_at FROM tracked_threads").fetchall()
        for name, created in legacy:
            conn.execute(
                "INSERT OR IGNORE INTO collections (name, query, watched, created_at)"
                " VALUES (?, ?, 1, ?)",
                (name, name, created),
            )
    except sqlite3.OperationalError:
        pass  # 旧表不存在(新装环境)
    conn.execute("DROP TABLE IF EXISTS tracked_threads")
    # 已标注登记:AI 标注成功即记录;存量补标注按"未登记"选目标(天然断点续跑)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annotated_sessions (
            session_id TEXT PRIMARY KEY,
            annotated_at REAL
        )
    """)
    # 目录时间线链接(符号链接):cwd 不在目标工作区的会话归入其时间线
    # (AI 归线写回 + 手动「归入目录线」);dir 按归一化键存储
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dir_links (
            dir TEXT NOT NULL,
            session_id TEXT NOT NULL,
            added_at REAL,
            PRIMARY KEY (dir, session_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dirl_session ON dir_links(session_id)")
    # 追踪目标目录(文件系统模型收口):目录获得与合集同等的追踪能力
    # (面板/列表色线/进度注记);dir=归一化键,display=原始大小写供显示
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_dirs (
            dir TEXT PRIMARY KEY,
            display TEXT,
            created_at REAL
        )
    """)
    # 瘦身死列清理(2026-10-15;列不存在 = 已是新形状,跳过)
    for _idx in ("idx_status", "idx_pinned"):
        conn.execute("DROP INDEX IF EXISTS " + _idx)
    for _t, _cols in (("sessions", ("status", "raw_meta", "pinned", "pinned_at")),
                      ("projects", ("status",))):
        for _c in _cols:
            try:
                conn.execute("ALTER TABLE " + _t + " DROP COLUMN " + _c)
            except sqlite3.OperationalError:
                pass


_MIGRATIONS = {1: _migrate_0_to_1}


def ensure_schema(conn):
    """按 PRAGMA user_version 逐版迁移到 SCHEMA_VERSION;已最新则零操作。"""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in range(v + 1, SCHEMA_VERSION + 1):
        _MIGRATIONS[target](conn)
        conn.execute("PRAGMA user_version = %d" % target)
    conn.commit()
