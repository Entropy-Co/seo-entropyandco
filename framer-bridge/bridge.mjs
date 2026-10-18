// Framer Server API bridge for the Python SEO engine.
//
// Framer's Server API ships only as a Node package (`framer-api`), so Python
// drives it through this script: one JSON request on stdin, one JSON response
// on stdout ({ ok: true, result } | { ok: false, error }).
//
// Commands:
//   inspect                      collections, fields (id/name/type/enum cases), item counts
//   list-items   {collection}    items with id, slug, draft, title
//   upsert-item  {collection, slug, draft, values, itemId?}
//                values: { "<Framer field name>": { value, contentType?, alt? } }
//   publish      {pathPrefix, allowUnrelated}   preview deployment (refuses unrelated pending changes)
//   deploy       {deploymentId}                 promote a reviewed deployment to the custom domain
//
// Credentials come from FRAMER_PROJECT_URL and FRAMER_API_KEY. Never log them.

import { connect } from "framer-api"

function fail(message) {
  process.stdout.write(JSON.stringify({ ok: false, error: message }))
  process.exit(1)
}

async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  const raw = Buffer.concat(chunks).toString("utf8").trim()
  return raw ? JSON.parse(raw) : {}
}

async function findCollection(framer, nameOrId) {
  const collections = await framer.getCollections()
  const match = collections.find((c) => c.id === nameOrId || c.name === nameOrId)
  if (!match) {
    const names = collections.map((c) => c.name).join(", ")
    throw new Error(`Collection "${nameOrId}" not found. Available: ${names}`)
  }
  return match
}

function describeField(field) {
  const out = { id: field.id, name: field.name, type: field.type }
  if (field.type === "enum" && Array.isArray(field.cases)) {
    out.cases = field.cases.map((c) => ({ id: c.id, name: c.name }))
  }
  return out
}

function fieldEntry(field, input) {
  const value = input?.value
  switch (field.type) {
    case "string":
      return { type: "string", value: String(value ?? "") }
    case "formattedText":
      return { type: "formattedText", value: String(value ?? ""), contentType: input?.contentType ?? "html" }
    case "date":
      return { type: "date", value: value ?? null }
    case "link":
      return { type: "link", value: value ?? null }
    case "image":
      return { type: "image", value: value ?? null, ...(input?.alt ? { alt: String(input.alt) } : {}) }
    case "boolean":
      return { type: "boolean", value: Boolean(value) }
    case "number":
      return { type: "number", value: Number(value) }
    case "enum": {
      const match = (field.cases ?? []).find((c) => c.id === value || c.name === value)
      if (!match) throw new Error(`Enum field "${field.name}" has no case "${value}"`)
      return { type: "enum", value: match.id }
    }
    default:
      throw new Error(`Field "${field.name}" has type "${field.type}", which this bridge does not write`)
  }
}

function itemTitle(item, titleFieldId) {
  const entry = titleFieldId ? item.fieldData?.[titleFieldId] : undefined
  return typeof entry?.value === "string" ? entry.value : ""
}

function changedPaths(changes) {
  if (!changes || typeof changes !== "object") return []
  const out = []
  for (const key of ["added", "removed", "modified"]) {
    const list = changes[key]
    if (Array.isArray(list)) out.push(...list.map((p) => (typeof p === "string" ? p : p?.path ?? String(p))))
  }
  return out
}

const commands = {
  async inspect(framer) {
    const info = await framer.getProjectInfo()
    const collections = []
    for (const collection of await framer.getCollections()) {
      const fields = await collection.getFields()
      const items = await collection.getItems()
      collections.push({
        id: collection.id,
        name: collection.name,
        managedBy: collection.managedBy,
        slugFieldName: collection.slugFieldName,
        fields: fields.map(describeField),
        itemCount: items.length,
      })
    }
    return { project: { id: info.id, name: info.name }, collections }
  },

  async "list-items"(framer, { collection }) {
    const target = await findCollection(framer, collection)
    const items = await target.getItems()
    return items.map((item) => ({
      id: item.id,
      slug: item.slug,
      draft: item.draft,
      title: itemTitle(item, target.slugFieldBasedOn),
      updatedAt: item.updatedAt ?? null,
    }))
  },

  async "upsert-item"(framer, { collection, slug, draft, values, itemId }) {
    if (!slug) throw new Error("slug is required")
    const target = await findCollection(framer, collection)
    if (target.managedBy !== "user") throw new Error(`Collection "${target.name}" is managed by a plugin and is read-only`)
    const fields = await target.getFields()
    const byName = new Map(fields.map((f) => [f.name, f]))
    const fieldData = {}
    for (const [name, input] of Object.entries(values ?? {})) {
      const field = byName.get(name)
      if (!field) throw new Error(`Field "${name}" not found. Available: ${fields.map((f) => f.name).join(", ")}`)
      fieldData[field.id] = fieldEntry(field, input)
    }
    const existing = (await target.getItems()).find((i) => i.slug === slug)
    if (existing && existing.id !== itemId) {
      throw new Error(`An item with slug "${slug}" already exists (id ${existing.id}); refusing to overwrite it`)
    }
    const payload = { slug, draft: Boolean(draft), fieldData }
    await target.addItems([itemId ? { id: itemId, ...payload } : payload])
    const saved = (await target.getItems()).find((i) => i.slug === slug)
    if (!saved) throw new Error(`Item "${slug}" was not found after the write`)
    return { id: saved.id, slug: saved.slug, draft: saved.draft, fieldsWritten: Object.keys(values ?? {}) }
  },

  async publish(framer, { pathPrefix, allowUnrelated }) {
    const changes = await framer.getChangedPaths()
    const paths = changedPaths(changes)
    const unrelated = pathPrefix ? paths.filter((p) => !p.startsWith(pathPrefix)) : []
    if (unrelated.length && !allowUnrelated) {
      throw new Error(
        `Refusing to publish: the project has pending changes outside ${pathPrefix}: ${unrelated.slice(0, 10).join(", ")}. ` +
          "Publishing would ship someone else's unreviewed edits.",
      )
    }
    const result = await framer.publish()
    return {
      changedPaths: paths,
      deployment: { id: result.deployment?.id, status: result.deployment?.status ?? null },
      hostnames: (result.hostnames ?? []).map((h) => ({ hostname: h.hostname, type: h.type, isPrimary: h.isPrimary })),
    }
  },

  async deploy(framer, { deploymentId }) {
    if (!deploymentId) throw new Error("deploymentId is required")
    const hostnames = await framer.deploy(deploymentId)
    return { hostnames: hostnames.map((h) => ({ hostname: h.hostname, isPrimary: h.isPrimary, isPublished: h.isPublished })) }
  },
}

async function main() {
  const command = process.argv[2]
  const handler = commands[command]
  if (!handler) fail(`Unknown command "${command}". Use one of: ${Object.keys(commands).join(", ")}`)
  const projectUrl = process.env.FRAMER_PROJECT_URL
  const apiKey = process.env.FRAMER_API_KEY
  if (!projectUrl || !apiKey) fail("FRAMER_PROJECT_URL and FRAMER_API_KEY must be set")

  const args = await readStdin()
  let framer
  try {
    framer = await connect(projectUrl, apiKey)
    const result = await handler(framer, args)
    process.stdout.write(JSON.stringify({ ok: true, result }))
  } catch (error) {
    fail(error instanceof Error ? error.message : String(error))
  } finally {
    if (framer) await framer.disconnect().catch(() => {})
  }
}

await main()
