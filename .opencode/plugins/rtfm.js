import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const rtfmPlugin = async ({ directory }) => {
  const pluginRoot = directory || path.resolve(__dirname, "../..");
  const skillsDir = path.resolve(pluginRoot, "skills");
  const launcher = path.resolve(pluginRoot, "bin/launch");

  return {
    config: async (config) => {
      config.skills = config.skills || {};
      config.skills.paths = config.skills.paths || [];
      if (!config.skills.paths.includes(skillsDir)) {
        config.skills.paths.push(skillsDir);
      }

      config.mcp = config.mcp || {};
      if (!config.mcp.rtfm) {
        config.mcp.rtfm = {
          type: "local",
          command: ["python3", launcher],
        };
      }
    },
  };
};
