import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightLinksValidator from "starlight-links-validator";
import mermaid from "astro-mermaid";

export default defineConfig({
  site: "https://awslabs.github.io",
  base: "/social-messaging-agent-runtime-toolkit",
  integrations: [
    // Client-side Mermaid: renders the ```mermaid fences in docs/ unchanged, with no
    // headless browser at build time. Must come before Starlight so it sees the fences first.
    mermaid({ autoTheme: true }),
    starlight({
      title: "SMART",
      description: "Social Messaging Agent Runtime Toolkit: build WhatsApp chatbots on AWS.",
      social: [
        { icon: "github", label: "GitHub", href: "https://github.com/awslabs/social-messaging-agent-runtime-toolkit" },
      ],
      // Fails the build on a broken internal link or anchor.
      plugins: [starlightLinksValidator()],
      sidebar: [
        {
          label: "Start here",
          items: ["getting-started", "architecture", "building-with-socialagent", "local-dev"],
        },
        {
          label: "Guides",
          items: [
            "agent-integration",
            "notifications",
            "templates",
            "events-scheduling",
            "media",
            "state",
            "message-buffering",
            "admin-console",
          ],
        },
        { label: "Reference", items: ["configuration", "c4-diagrams"] },
        { label: "Security", items: ["threat-model", "data-flows"] },
        {
          label: "Decisions",
          items: ["architecture-decisions", { label: "Architecture Decision Records", items: [{ autogenerate: { directory: "adr" } }] }],
        },
      ],
    }),
  ],
});
