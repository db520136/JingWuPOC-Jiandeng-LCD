# -*- coding: utf-8 -*-
"""最小 UI 导航验证入口，不启动现有业务服务。"""

print("[NavTest] entry")
try:
    from usr.ui_app import main
except ImportError:
    from ui_app import main

print("[NavTest] ui_app imported")


if __name__ == "__main__":
    print("[NavTest] calling main")
    main()
