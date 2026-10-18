"""存储门面:IndexDB = 五域混入 + 基座;导出面保持 IndexDB / SessionRecord /
_norm_dir 不变——调用方(deps/main/mcp_server/services/tests)零改动。

2026-10-15 自 1011 行单体拆出(方法体 AST 原样搬移,零逻辑改动):
  base.py        SessionRecord/连接/事务/目录归一化
  schema.py      user_version 版本化迁移(替代 try/except ALTER 自愈)
  sessions.py    会话 CRUD/幽灵清理/归档回退/标注写回/扫描日志/统计
  projects.py    项目同步与查询
  collections.py 合集+目录链接+追踪+策展(FS 成员模型域)
  tags.py        标签
  fts.py         内容全文索引与搜索
"""
from storage.base import StorageBase, _norm_dir, SessionRecord
from storage.sessions import SessionsMixin
from storage.projects import ProjectsMixin
from storage.collections import CollectionsMixin
from storage.tags import TagsMixin
from storage.fts import FtsMixin


class IndexDB(SessionsMixin, ProjectsMixin, CollectionsMixin, TagsMixin, FtsMixin, StorageBase):
    """组合门面:保持 storage.index.IndexDB 导入契约(继承面即全部能力)。"""
