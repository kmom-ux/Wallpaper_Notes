from dataclasses import dataclass


@dataclass
class NoteInfo:
    """单个便签的数据表示。

    filepath:  .md 文件的完整路径
    filename:  显示用名称（不含 .md 后缀）
    content:   文件内容（原始 Markdown 文本）
    last_modified:  文件最后修改时间戳（time.time() 格式）
    """
    filepath: str
    filename: str
    content: str
    last_modified: float
