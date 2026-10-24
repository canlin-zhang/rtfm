import { readFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin, Skill } from "@opencode/plugin"

const SERVER_NAME = "rtfm"
const root = dirname(fileURLToPath(import.meta.url))

async function loadSkill(skillPath: string) {
  let raw: string
  try {
    raw = await readFile(skillPath, "utf8")
  } catch (e) {
    throw new Error(
      `[rtfm] cannot load ${skillPath}: ${e}. The plugin install is incomplete; reinstall the rtfm plugin.`,
    )
  }
  const frontmatter = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/)
  const description = frontmatter?.[1].match(/^description:\s*(.+)$/m)?.[1]
  if (!frontmatter || !description) {
    throw new Error(
      `[rtfm] ${skillPath}: expected "---" frontmatter with a single-line description`,
    )
  }
  return { description, content: raw.slice(frontmatter[0].length) }
}

export default Plugin.define({
  id: "rtfm",
  async setup(ctx) {
    // Load the skill before registering the MCP server: a broken skill must
    // abort the whole setup, not leave a half-registered plugin.
    const skillPath = join(root, "skills/read-the-manual/SKILL.md")
    const { description, content } = await loadSkill(skillPath)

    await ctx.mcp.transform((editor) => {
      editor.set(SERVER_NAME, {
        type: "local",
        command: ["python3", join(root, "bootstrap.py"), "--front", "opencode"],
      })
    })

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
