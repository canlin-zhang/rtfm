import { readFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin, Skill } from "@opencode/plugin"

const root = dirname(fileURLToPath(import.meta.url))

export default Plugin.define({
  id: "rtfm",
  async setup(ctx) {
    await ctx.mcp.transform((editor) => {
      editor.set("rtfm", {
        type: "local",
        command: ["python3", join(root, "bootstrap.py"), "--front", "opencode"],
      })
    })

    const skillPath = join(root, "skills/read-the-manual/SKILL.md")
    const raw = await readFile(skillPath, "utf8")
    const description = raw.match(/^description:\s*(.+)$/m)?.[1] ?? "Read the manual"
    const content = raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "")

    await ctx.skill.transform((editor) => {
      editor.add({
        id: Skill.ID.make("read-the-manual"),
        name: Skill.Name.make("Read the Manual"),
        description,
        path: Skill.Info.fields.path.make(skillPath),
        content,
      })
    })
  },
})
