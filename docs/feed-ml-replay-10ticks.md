# Feed ML replay: 10 historical ticks

This replay compares what the historical proactive tick selected with what the
current trained ML funnel would select at the same tick time.

Label meanings:

- `interesting`: historical LLM marked the item interesting, or the item already has `interest_ok = 1`.
- `not_interesting`: historical LLM marked the item not interesting, or the item already has `interest_ok = 0`.
- `unknown`: this candidate was not labeled in history, so it must not be counted as either good or bad.

High level result:

```text
10 tick replay
├─ historical old selections
│  ├─ known labels: 44
│  └─ interesting rate: 0.32
└─ current trained ML funnel selections
   ├─ known labels: 25
   └─ interesting rate on known labels: 0.35
```

Important caveat:

```text
evaluation caveat
├─ old selected items have direct LLM labels
├─ new selected items often were never shown historically
└─ unknown new labels cannot be treated as negative
```

## Tick 1: `205d9d6b`

Started at `2026-07-06T21:01:22.226160+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: In neuroscience, global workspace theory holds that thoughts |
| interesting | AnthropicAI Twitter Feed | New Anthropic research: A global workspace in language models. Of everything hap |
| interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: The J-space (named after the Jacobian, the mathematical techn |
| not_interesting | VGC News | Nintendo of Europe will stop selling the Nintendo Switch in 2027 |
| not_interesting | Claude Blog | A Field Guide to Claude Fable: Finding Your Unknowns |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| interesting | Karpathy (Twitter) | RT by @karpathy: I spent a LOT of time through the hardest 3D prompts at Fable, |
| interesting | HLTV News | XSE releases statement regarding issues at XSE Pro League Guangzhou |
| not_interesting | Claude Blog | A Field Guide to Claude Fable: Finding Your Unknowns |
| interesting | Claude Blog | A field guide to Claude Fable 5: Finding your unknowns |
| interesting | Cloudflare Blog | Your Worker can now have its own cache in front of it |

## Tick 2: `2dfa3e12`

Started at `2026-07-06T21:24:39.303605+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: In neuroscience, global workspace theory holds that thoughts |
| interesting | AnthropicAI Twitter Feed | New Anthropic research: A global workspace in language models. Of everything hap |
| interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: The J-space (named after the Jacobian, the mathematical techn |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| interesting | Karpathy (Twitter) | RT by @karpathy: I spent a LOT of time through the hardest 3D prompts at Fable, |
| interesting | arXiv q-bio.NC - 每日神经科学论文 | A frozen rate operator from the complete larval connectome: degree and weight go |
| interesting | HLTV News | XSE releases statement regarding issues at XSE Pro League Guangzhou |
| interesting | Claude Blog | A field guide to Claude Fable 5: Finding your unknowns |
| interesting | Cloudflare Blog | Your Worker can now have its own cache in front of it |

## Tick 3: `3a253294`

Started at `2026-07-06T21:48:25.915811+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: In neuroscience, global workspace theory holds that thoughts |
| not_interesting | AnthropicAI Twitter Feed | New Anthropic research: A global workspace in language models. Of everything hap |
| not_interesting | PC Gamer UK - Games | CD Projekt Red thanks 6000 Edgerunners 2 advance viewers for not spoiling the sh |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: The J-space (named after the Jacobian, the mathematical techn |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| interesting | Karpathy (Twitter) | RT by @karpathy: I spent a LOT of time through the hardest 3D prompts at Fable, |
| interesting | arXiv q-bio.NC - 每日神经科学论文 | A frozen rate operator from the complete larval connectome: degree and weight go |
| interesting | HLTV News | XSE releases statement regarding issues at XSE Pro League Guangzhou |
| interesting | Claude Blog | A field guide to Claude Fable 5: Finding your unknowns |
| interesting | Cloudflare Blog | Your Worker can now have its own cache in front of it |

## Tick 4: `4a49e128`

Started at `2026-07-07T03:14:24.783124+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| interesting | GitHub Trending Daily | alibaba/zvec |
| not_interesting | GitHub Trending Daily | sindresorhus/awesome |
| not_interesting | GitHub Trending Daily | karakeep-app/karakeep |
| not_interesting | PC Gamer UK - Games | How do you reconcile your lore-heavy, dark sci-fi MMO with mod tools that might |
| not_interesting | GitHub Trending Daily | bradautomates/claude-video |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | Image |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: We also partnered with Neuronpedia to create an interactive d |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | On a Geometry of Interbrain Networks |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | BioSEN: A Bio-acoustic Signal Enhancement Network for Animal Vocalizations |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | Human-like Object Grouping in Self-supervised Vision Transformers |

## Tick 5: `22935ab0`

Started at `2026-07-07T03:24:53.112346+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| interesting | GitHub Trending Daily | alibaba/zvec |
| interesting | Claude Blog | A field guide to Claude Fable 5: Finding your unknowns |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | Image |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: We also partnered with Neuronpedia to create an interactive d |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | On a Geometry of Interbrain Networks |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | BioSEN: A Bio-acoustic Signal Enhancement Network for Animal Vocalizations |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | Human-like Object Grouping in Self-supervised Vision Transformers |

## Tick 6: `19b3d7b1`

Started at `2026-07-07T04:27:31.267865+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| not_interesting | arXiv cs.HC - 每日人机交互论文 | A Comparative Study of Static, Scrollytelling, and Chatbot Visualization Onboard |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Regulating AI: Where U.S. State Policy and HCI (Mis)align |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Between Knowledge and Care: A Mixed-Methods Evaluation of Generative AI for T2DM |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Doom Researching: A Conceptual Framework for Repetitive AI-Assisted Information |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Evaluating Affective Objectives: Statistical Numbing in Data Visualization |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | Image |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: We also partnered with Neuronpedia to create an interactive d |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | On a Geometry of Interbrain Networks |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | BioSEN: A Bio-acoustic Signal Enhancement Network for Animal Vocalizations |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | Human-like Object Grouping in Self-supervised Vision Transformers |

## Tick 7: `232b1261`

Started at `2026-07-07T04:41:49.814388+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| interesting | arXiv cs.HC - 每日人机交互论文 | Enactive Drift Regulation and the Emergence Machine: A Framework for Coherent Ad |
| interesting | arXiv cs.HC - 每日人机交互论文 | Scalable Semantic Steering of Embedding Projections |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | The New Shape of Search: How Conversational AI Recomposes Information Seeking |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | CoGen3D: An Agentic Human-AI Co-Design Pipeline for 3D Asset Generation for Virt |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Post-Lecture Interactive Environments for Conceptual Learning: A Randomized Comp |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | Image |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: We also partnered with Neuronpedia to create an interactive d |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | On a Geometry of Interbrain Networks |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | BioSEN: A Bio-acoustic Signal Enhancement Network for Animal Vocalizations |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | Human-like Object Grouping in Self-supervised Vision Transformers |

## Tick 8: `399a29da`

Started at `2026-07-07T04:51:40.814067+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Who Responds When the Driver Is Gone? A Framework for Human Intent Understanding |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | The ABC of digital health: A framework for translating digital health interventi |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | The User-In-Context Framework: Understanding Variation in How Users Respond to A |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | Identifying Deceptive Patterns Across Three Age Groups: A Heuristic-Based Cognit |
| not_interesting | arXiv cs.HC - 每日人机交互论文 | From Interaction to Intent: Inferring User Objectives from Provenance Logs |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | Image |
| not_interesting | AnthropicAI Twitter Feed | R to @AnthropicAI: We also partnered with Neuronpedia to create an interactive d |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | On a Geometry of Interbrain Networks |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | BioSEN: A Bio-acoustic Signal Enhancement Network for Animal Vocalizations |
| unknown | arXiv q-bio.NC - 每日神经科学论文 | Human-like Object Grouping in Self-supervised Vision Transformers |

## Tick 9: `90878cdc`

Started at `2026-07-07T05:29:58.274147+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| not_interesting | Tibo Sottiaux (Twitter) | New moon. New models. Welcome GPT-5.6 Sol, currently in limited preview. |
| not_interesting | terasumc (Artist) | RT by @terasumc: 2人の近況報告です🐻🐈 仲良くやってるみたいです👻 |
| not_interesting | terasumc (Artist) | RT @curomogu: おちんちんばっかり成長するの嫌だなぁ…前貼りで隠すの大変だし… |
| not_interesting | terasumc (Artist) | RT @ti_jiyuugyou: 媚びず、下手（したて）には出ない。自分の価値をわかってて |
| not_interesting | terasumc (Artist) | RT @anon200million2: 女友達に性癖バレてちんちん嗅がれる漫画の続編描いてます |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| unknown | NiKo (X/Twitter) - Final Test | Cologne major VLOG is finally out, full video on my YT 🙏🏻❤️ |
| unknown | NiKo (X/Twitter) - Final Test | New video uploaded, should be good now. Go check it out on my YT 🙏🏻 |
| unknown | NiKo (X/Twitter) - Final Test | R to @NiKoCS_: youtu.be/7etNh7676O8 |
| unknown | NiKo (X/Twitter) - Final Test | #NewProfilePic |
| unknown | NiKo (X/Twitter) - Final Test | Holy shit man |

## Tick 10: `f5dcfd40`

Started at `2026-07-07T05:38:07.906037+00:00`.

Historical selection:

| label | source | title |
|---|---|---|
| not_interesting | terasumc (Artist) | RT by @terasumc: おちんちんばっかり成長するの嫌だなぁ…前貼りで隠すの大変 |
| not_interesting | terasumc (Artist) | RT by @terasumc: 女友達に性癖バレてちんちん嗅がれる漫画の続編描いてます |
| not_interesting | terasumc (Artist) | RT by @terasumc: ビッチギャル vs. デカチン童貞（1/4） |
| not_interesting | terasumc (Artist) | RT by @terasumc: 媚びず、下手（したて）には出ない。自分の価値をわかってて |
| not_interesting | terasumc (Artist) | RT by @terasumc: テラスMC(@terasumc)さんのカーヤさんのラクガキ。 |

Current ML funnel selection:

| label | source | title |
|---|---|---|
| unknown | NiKo (X/Twitter) - Final Test | Cologne major VLOG is finally out, full video on my YT 🙏🏻❤️ |
| unknown | NiKo (X/Twitter) - Final Test | New video uploaded, should be good now. Go check it out on my YT 🙏🏻 |
| unknown | NiKo (X/Twitter) - Final Test | R to @NiKoCS_: youtu.be/7etNh7676O8 |
| unknown | NiKo (X/Twitter) - Final Test | #NewProfilePic |
| unknown | NiKo (X/Twitter) - Final Test | Holy shit man |

## Observations

```text
observed behavior
├─ clear win
│  ├─ ticks 1-3 improve or keep high known-interest rate
│  └─ model replaces repeated weak items with previously positive themes
├─ unclear
│  ├─ ticks 4-8 choose many unlabeled q-bio items
│  └─ unknown labels need live LLM judgment before scoring
└─ risk
   └─ ticks 9-10 concentrate on one source, NiKo, after avoiding terasumc noise
```

Recommended next step:

```text
next iteration
├─ keep ML funnel
├─ add source cap in final rerank
│  └─ e.g. max 2 items per source in top 5
├─ add exploration slot
│  └─ one fresh high-coarse unknown item
└─ run live dev_verify to let LLM label unknown new choices
```
