// Bilibili dynamic card template.
// All dynamic data is injected through `sys.inputs.data` as JSON so user text
// is never parsed as Typst markup.
#set page(
  width: 960pt,
  height: auto,
  margin: 0pt,
  fill: gradient.linear(rgb("fff6fa"), rgb("f4f8ff"), angle: 90deg),
)
#set text(
  font: (
    "WenQuanYi Micro Hei",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "PingFang SC",
    "Arial Unicode MS",
  ),
  size: 29pt,
  fill: rgb("23262f"),
  cjk-latin-spacing: none,
)
#set par(leading: 0.75em, spacing: 1.1em)

#let d = json(bytes(sys.inputs.data))

#let pink = rgb("fb7299")
#let secondary = rgb("626876")
#let muted = rgb("949aa8")
#let border = rgb("e8ecf5")
#let soft = rgb("f7f9fd")

#let frame(path, w, h, r) = block(
  clip: true,
  radius: r,
  width: w,
  height: h,
  if path == "" {
    rect(width: w, height: h, fill: rgb("eef2f8"))
  } else {
    image(path, width: w, height: h, fit: "cover")
  },
)

// Render text tokens: plain text plus inline images (emoji and icons).
#let rich(tokens, size: 1.05em) = {
  for token in tokens {
    if "image" in token {
      box(baseline: -0.15em, image(token.image, height: size))
    } else {
      token.text
    }
  }
}

#let stat(s) = block(
  width: auto,
  radius: 19pt,
  fill: soft,
  stroke: 0.6pt + border,
  inset: (x: 16pt, y: 7pt),
  grid(
    columns: (auto, auto),
    column-gutter: 7pt,
    align: horizon,
    rich(s.icon, size: 1.15em),
    text(size: 22pt, fill: secondary)[#s.value],
  ),
)

#block(width: 100%, inset: 24pt)[
  #block(
    width: 100%,
    radius: 28pt,
    fill: white,
    stroke: 1pt + border,
    inset: 30pt,
  )[
    #grid(
      columns: (auto, 1fr, auto),
      gutter: 20pt,
      align: horizon,
      frame(d.avatar, 76pt, 76pt, 38pt),
      [
        #text(size: 32pt, weight: "bold")[#d.author]
        #linebreak()
        #text(size: 22pt, fill: secondary)[#d.uid]
      ],
      text(size: 20pt, fill: muted)[#d.published],
    )
    #v(14pt)
    #line(length: 100%, stroke: 0.6pt + border)
    #v(10pt)

    #if d.text.len() > 0 [
      #rich(d.text)
      #v(10pt)
    ]

    #if d.video != none [
      #block(
        width: 100%,
        radius: 24pt,
        fill: soft,
        stroke: 0.6pt + border,
        inset: 22pt,
      )[
        #grid(
          columns: (336pt, 1fr),
          gutter: 24pt,
          align: horizon,
          frame(d.video.cover, 336pt, 210pt, 18pt),
          [
            #text(size: 32pt, weight: "bold")[#d.video.title]
            #v(12pt)
            #if d.video.duration != "" [
              #block(
                width: auto,
                radius: 18pt,
                fill: white,
                stroke: 0.6pt + border,
                inset: (x: 17pt, y: 3pt),
                text(size: 22pt, fill: secondary)[时长 #d.video.duration],
              )
            ]
          ],
        )
        #if d.video.desc != "" [
          #v(12pt)
          #line(length: 100%, stroke: 0.6pt + border)
          #v(6pt)
          #text(size: 18pt, fill: muted)[简介]
          #v(2pt)
          #text(size: 22pt, fill: secondary)[#d.video.desc]
        ]
      ]
      #v(12pt)
    ]

    #if d.images.len() > 0 [
      #grid(
        columns: (168pt, 168pt, 168pt),
        gutter: 12pt,
        ..d.images.map(path => frame(path, 168pt, 168pt, 16pt)),
      )
      #v(12pt)
    ]

    #if d.orig != none [
      #block(
        width: 100%,
        radius: 22pt,
        fill: soft,
        stroke: 0.6pt + border,
        inset: 24pt,
      )[
        #text(size: 22pt, fill: pink)[转发原动态]
        #v(6pt)
        #text(size: 22pt, fill: secondary)[@#d.orig.author]
        #v(4pt)
        #text(size: 22pt)[#d.orig.summary]
      ]
      #v(12pt)
    ]

    #if d.stats.len() > 0 [
      #grid(
        columns: (auto, auto, auto),
        gutter: 14pt,
        ..d.stats.map(s => stat(s)),
      )
      #v(12pt)
    ]

    #line(length: 100%, stroke: 0.6pt + border)
    #v(6pt)
    #grid(
      columns: (1fr, auto),
      text(size: 18pt, fill: muted)[#d.footer_left],
      text(size: 18pt, fill: muted)[#d.footer_right],
    )
  ]
]
