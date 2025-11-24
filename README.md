# 🧠 OES GenAI Micro-Apps — Production Repository

**Last Updated:** 2025-11-24

**Maintained by:** **Imaad Fakier — Senior GenAI Developer, OES**

This repository contains the **production-ready** suite of GenAI micro-applications used inside OES for internal operations, instructional design workflows, and accessibility support.

It represents the **final stable layer** of the OES GenAI ecosystem:

- audited,
- security-aligned,
- deterministic,
- with tested UX,
- and minimal moving parts.

---

## 🚀 Purpose of This Repository

**AI-MicroApps-main is not a sandbox.**

It is the **deployment-ready** environment used by OES teams to:

- Run high-impact instructional and accessibility tools
- Process Storyboards into Canvas courses
- Generate learning assets for production delivery
- Interact with vetted LLM pipelines
- Support RAG-based workflows
- Maintain enduring knowledge tools

All apps here:

- Follow OES GenAI architectural standards
- Use our unified dependency stack
- Implement secure access control
- Include complete inline documentation

---

## 📁 Repository Structure (production)

Only currently active apps are kept here.

```text
AI-MicroApps-main/
│
├── app_alt_text_construct.py
├── app_construct_lo_generator.py
├── app_discussion_generator.py
├── app_image_latex.py
├── app_image_text.py
├── app_mg_script_gen.py
├── app_ptc_video_script_gen.py
├── app_quiz_question_gen.py
├── app_scenario_video_script.py
├── umich_feedback_bot.py
├── visual_transcripts.py
│
├── core_logic/
│   ├── handlers.py
│   ├── llm_config.py
│   ├── main.py
│   ├── rag_pipeline.py
│   └── data_storage.py
│
├── app_images/
├── rag_docs/
├── shared_assets/
│
├── requirements.txt
├── LICENSE
└── README.md
```

---

## 🧩 Core Production Apps

| App                                   | Description                                                        |
| ------------------------------------- | ------------------------------------------------------------------ |
| **visual_transcripts.py**             | Precision transcript generator with SRT alignment and editable UX. |
| **umich_feedback_bot.py**             | CAI-aligned elaborative feedback (Michigan pilot).                 |
| **app_quiz_question_gen.py**          | Structured quiz generator; LO-aware.                               |
| **app_discussion_generator.py**       | Canvas discussion prompts.                                         |
| **app_construct_lo_generator.py**     | CLD-driven LO builder.                                             |
| **app_alt_text_construct.py**         | WCAG accessibility alt-text generator.                             |
| **image + latex suite**               | Converts image → structured instructional content.                 |
| **scenario + micro-learning scripts** | Pre-tutorial content / instructional video generation.             |

---

## 🧱 Shared Architecture (Production Rules)

### 1️⃣ Single shared core

All apps rely on:

```text
core_logic/
```

Never duplicate logic.

### 2️⃣ Unified dependencies

Pinned + deterministic:

- OpenAI SDK v1
- LangChain 0.3.x LCEL
- MongoDB vector store architecture

### 3️⃣ Stable UI/UX

Apps must remain:

- predictable,
- minimally configurable,
- accessible to non-technical users.

### 4️⃣ Access control

No unauthenticated usage.

---

## 🔐 Security Model

Production secrets must never exist locally.

Use:

- Streamlit Secrets Manager
- OES secure vault infrastructure
- Environment-hashed access codes

---

## ⚙️ Deployment Expectations

- Zero experimental code
- No non-functional modules
- No partial migrations
- Every function fully documented

---

## 🔄 Promotion Path

- **AI-MicroApps-test → AI-MicroApps-main**

  - Only after:

    - refactor is complete
    - user feedback implemented
    - architecture validated
    - UX tested by LD stakeholders
    - dependencies stabilized

---

## 🧭 Governance

This repo falls under the umbrella of:

- **Snowflake Ownership & Maintenance (OES GenAI)**

  - All apps tracked as digital assets
  - Standardized & auditable
  - Attached to operational capacity models

---

## 📄 License

Internal proprietary OES GenAI tooling.
External use strictly prohibited.

---

## 💬 Maintainer

**Imaad Fakier**
Senior GenAI Developer — OES
📧 [ifakier@oes.com](mailto:ifakier@oes.com)

> **“Where instructional AI meets real production workflows.”**
