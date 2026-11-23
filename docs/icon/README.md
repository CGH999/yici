# 应用图标

DeepSeek 鲸鱼娘（`DSniang1.png`，原图来自 `dsh-whale-widget` 的资源，已复制到本目录）
外加一个白色对话气泡和单词 `Aa`。

```
docs/icon/
├─ DSniang1.png   角色原图（610×610，透明背景）
├─ render.html    生成源（HTML + CSS），改字/改位置只改这个
├─ preview.png    1024×1024 成品
└─ README.md
```

项目里用的 `app/static/icon-{32,64,180,192,512}.png` 都是从 `preview.png` 缩出来的。

## 重新生成

在本目录下执行：

```bash
# 1. 渲染 1024 原图（Chrome 无头，透明背景）
chrome --headless=new --disable-gpu --no-sandbox --default-background-color=00000000 \
  --hide-scrollbars --window-size=1024,1024 \
  --screenshot=preview.png render.html

# 2. 缩出各尺寸
python -c "from PIL import Image; im=Image.open('preview.png'); \
[im.resize((s,s), Image.LANCZOS).save(f'../../app/static/icon-{s}.png') for s in (512,192,180,64,32)]"
```

`render.html` 里的角色图是**相对路径** `DSniang1.png`，所以整个目录可以直接拷走。

## 改文字

`render.html` 里这一句：

```html
<div class="bubble"><span>Aa</span></div>
```

换成 `A`、`译` 或者别的都行，字号在 `.bubble` 的 `font-size` 里调。
