# -*- coding: utf-8 -*-
"""只处理子页面左边缘右滑返回的轻量手势识别。"""


class EdgeBackGesture:
    def __init__(self, on_back, edge_px=32, distance_px=40):
        self.on_back = on_back
        self.edge_px = int(edge_px)
        self.distance_px = int(distance_px)
        self.start = None
        self.cancelled = False

    def begin(self, x, y):
        self.start = (int(x), int(y))
        self.cancelled = self.start[0] > self.edge_px

    def move(self, x, y):
        if self.start is None or self.cancelled:
            return
        dx = int(x) - self.start[0]
        dy = abs(int(y) - self.start[1])
        if dx < 0 or dy > abs(dx) * 2:
            self.cancelled = True

    def end(self, x, y):
        if self.start is None:
            return False
        dx = int(x) - self.start[0]
        dy = abs(int(y) - self.start[1])
        accepted = (not self.cancelled and dx >= self.distance_px and
                    dx > dy * 1.2)
        self.start = None
        self.cancelled = False
        if accepted:
            self.on_back()
        return accepted

