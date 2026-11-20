"""Translations for the task panel. Blender supplies the active locale."""

labels = (
    ("Server", "服务器", "サーバー"),
    ("Personal token", "个人令牌", "個人トークン"),
    ("Connect", "连接", "接続"),
    ("Read model provider terms", "阅读模型提供方条款", "モデル提供元の利用条件を読む"),
    (
        "I accept the provider terms for noncommercial use",
        "我同意提供方条款，仅作非商用",
        "提供元の条件に同意し、非商用で利用します",
    ),
    ("Confirm acceptance", "确认同意", "同意を確定"),
    ("Video", "视频", "動画"),
    ("Model", "模型", "モデル"),
    ("Start (s)", "开始（秒）", "開始（秒）"),
    ("End (s, 0 = full clip)", "结束（秒，0 表示片尾）", "終了（秒、0 は動画の末尾）"),
    ("Submit reconstruction", "提交重建", "再構成を依頼"),
    ("Refresh jobs", "刷新任务", "ジョブを更新"),
    ("Newer jobs", "较新的任务", "新しいジョブ"),
    ("Older jobs", "较早的任务", "以前のジョブ"),
    (
        "Import motion and set scene timing",
        "导入动作并设置场景帧率和范围",
        "モーションを読み込み、シーンの時間設定を更新",
    ),
    ("Motion imported", "动作已导入", "モーションを読み込みました"),
    ("Cancel job", "取消任务", "ジョブをキャンセル"),
    ("Retry job", "重新排队", "再試行"),
    ("queued", "排队中", "待機中"),
    ("running", "运行中", "実行中"),
    ("succeeded", "已完成", "完了"),
    ("failed", "失败", "失敗"),
    ("cancelled", "已取消", "キャンセル済み"),
    ("cancelling", "正在取消", "キャンセル中"),
    ("Checking video", "正在检查视频", "動画を確認中"),
    ("Downloading motion", "正在下载动作", "モーションをダウンロード中"),
    ("Reconstruction options", "重建选项", "再構成オプション"),
    ("Low memory", "低显存模式", "低メモリモード"),
    ("Contact correction", "接触修正", "接触補正"),
    ("Static camera", "固定镜头", "固定カメラ"),
    ("Smooth motion", "动作平滑", "モーションの平滑化"),
    ("Refine body (HTD)", "身体优化（HTD）", "体の補正（HTD）"),
    ("Refine ankles (FootMR)", "脚踝优化（FootMR）", "足首の補正（FootMR）"),
    ("Track fingers (HandFlow)", "手指跟踪（HandFlow）", "指の追跡（HandFlow）"),
)
messages = {
    locale: {(context, row[0]): row[i] for context in ("*", "Operator") for row in labels}
    for locale, i in (("zh_HANS", 1), ("ja_JP", 2))
}
