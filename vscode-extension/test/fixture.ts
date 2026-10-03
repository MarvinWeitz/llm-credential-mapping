const args = process.argv.slice(2);
const mode = args[0]?.startsWith("--fixture=") ? args.shift()?.slice(10) : "normal";
if (mode === "fail") {
  process.stderr.write("synthetic-secret-error");
  process.exitCode = 7;
} else if (mode === "hang") {
  setInterval(() => {}, 1000);
} else if (mode === "oversize") {
  process.stdout.write("x".repeat(1_048_577));
} else if (mode === "args") {
  console.log(JSON.stringify(args));
} else {
  if (args[0] === "--home") args.splice(0, 2);
  if (args[0] === "project" && args[1] === "list") {
    console.log(JSON.stringify({
      version: 1, active: "fixture-a",
      projects: ["fixture-a", "fixture-b"].map((name) => ({
        name, gateway: process.env.LLM_MAPPING_TEST_GATEWAY ?? "https://gateway.example.test",
        team_id: "team-" + name, allow_http: Boolean(process.env.LLM_MAPPING_TEST_GATEWAY),
      })),
    }));
  } else if (args[0] === "token") {
    const project = args[args.indexOf("--project") + 1];
    if (mode === "warn") process.stderr.write("synthetic-sensitive-warning");
    console.log(mode === "bad-token" ? "banner\nsynthetic-token" : "synthetic-token-" + project);
  } else if (args[0] === "status") {
    console.log(JSON.stringify({ state: "ready", user_id: "synthetic-user" }));
  } else {
    process.stderr.write("Unknown fixture command");
    process.exitCode = 2;
  }
}
