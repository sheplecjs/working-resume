# Working Resume

This is a personal project I use to expedite tailoring resumes to roles. Aside from the basics, it requires the following to function:

- ```content_bank.yaml``` - see the section below for an example
- ```resume.yaml``` - a base resume in the [rendercv](https://github.com/rendercv/rendercv) style
- a .env file with ```anthropic_api_key="..."```
- job description(s) in jds/ (pdf or md)

Entrypoint: ```task``` (requires [Task](https://taskfile.dev/))

### Content Bank

A content bank provides concrete examples of acceptable profiles and highlights from positions at ```content_bank.yaml```.

For example:

```yaml
profiles:
  - id: secret-agent-general
    text: >-
      Results-driven intelligence professional with 60+ years of field experience eliminating existential threats to Western civilization — often before the canapés are served. Adept at high-stakes negotiation, improvised vehicle operation, and maintaining a pressed suit in conditions that would destroy lesser men. Fluent in charm, fluent in menace; frequently fluent in both simultaneously. Holds an impeccable record of mission success despite a well-documented habit of announcing himself to adversaries before subduing them. Licensed to kill; also licensed to drive, fly, ski, and somehow always find the right tuxedo. Seeking a role with competitive benefits, occasional submarine access, and a Q Branch expense account.
    suitable_for:
      - special_agent
      - secret_agent
      - super_secret_agent

    ...

highlights:

  - id: mi6-world-saving
    company: MI6
    position: Special Agent
    status: active
    text: >-
      Thwarted 12+ global domination attempts by a rotating cast of disfigured megalomaniacs, achieving a 100% save-the-world rate despite routinely being captured in Act II
    tags:
      - world_saving
      - anti_doomsday
    suitable_for:
      - special_agent
      - mercenary

  ...
```