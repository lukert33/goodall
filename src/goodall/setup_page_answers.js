// The setup page's portable part: the answers, the checks on them, and the
// answers file they become. goodall.setup_page copies this file, unchanged,
// into the setup page it writes, ahead of the page's own script.
//
// Why it is a file of its own: nothing here touches the page (no document,
// no window), so tests/test_setup_page_script.py can run these same
// functions with the JavaScript that comes with macOS (osascript -l
// JavaScript) and hand the text answersFileText returns to goodall's own
// reader, without a browser.
//
// The page decides nothing goodall does not check again. Every check here
// mirrors one in goodall.answers_file, only so a person sees a problem
// while still on the page; goodall reads the saved file with its own rules,
// and those are the ones that count. When a rule changes there, change it
// here too, and the tests say so if the two disagree on the samples.

// The newest answers file layout this page writes (docs/answers-file.md).
var ANSWERS_SCHEMA_VERSION = 1;

// The same patterns goodall.eval_config and goodall.secrets_manager use.
var NAME_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;
var EMAIL_ADDRESS_PATTERN = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+/;
var VARIABLE_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
var REFERENCE_PART = "[A-Za-z0-9_.\\-](?:[A-Za-z0-9_.\\- ]*[A-Za-z0-9_.\\-])?";
var REFERENCE_PATTERN = new RegExp(
  "^op://" + REFERENCE_PART + "/" + REFERENCE_PART + "(?:/" + REFERENCE_PART + ")?/" + REFERENCE_PART + "$"
);
var REPOSITORY_PART_PATTERN = /^[A-Za-z0-9._-]+$/;

// The starts of the key formats goodall recognizes (goodall.quoting). A
// rough copy on purpose: it only has to catch a key pasted into the wrong
// box before goodall's own, stricter check refuses the whole file.
var KEY_STARTS = [
  /(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}/,
  /(^|[^A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}/,
  /(^|[^A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}/,
  /(^|[^A-Za-z0-9])(AKIA|ASIA)[A-Z0-9]{16}/,
  /(^|[^A-Za-z0-9])xox[bpa]-[A-Za-z0-9-]{20,}/,
  /(^|[^A-Za-z0-9])AIza[A-Za-z0-9_-]{35}/,
  /(^|[^A-Za-z0-9])ops_[A-Za-z0-9+\/=_-]{50,}/,
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
  /(^|[^A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/
];

// The variable a 1Password service account token is read from. It can never
// be a reference, so goodall refuses it in the env file.
var SERVICE_ACCOUNT_TOKEN_VARIABLE = "OP_SERVICE_ACCOUNT_TOKEN";

// The page's working copy of the answers, made from the starting answers
// goodall wrote into the page. `edited` remembers which suggested values
// the person has typed over, so a suggestion stops following the name it
// was made from once the person has chosen their own.
function startingState(starting) {
  var answers = starting.answers;
  var references = [];
  for (var index = 0; index < answers.secret_references.length; index++) {
    var row = answers.secret_references[index];
    references.push({ name: row.name, reference: row.reference });
  }
  return {
    account_mode: answers.account_mode,
    eval: answers.eval,
    new_eval: {
      name: answers.new_eval.name,
      description: answers.new_eval.description,
      workstreams: answers.new_eval.workstreams.slice(),
      construct: answers.new_eval.construct,
      activitywatch: answers.new_eval.activitywatch,
      repositories: answers.new_eval.repositories.slice()
    },
    contributor: {
      id: answers.contributor.id,
      display_name: answers.contributor.display_name,
      main_workstream: answers.contributor.main_workstream
    },
    folders: {
      record_repo: answers.folders.record_repo,
      log_directory: answers.folders.log_directory
    },
    uses_api_keys: references.length > 0,
    secret_references: references,
    goodall_program: answers.goodall_program,
    known_workstreams: starting.known_workstreams.slice(),
    edited: {
      contributor_id: answers.contributor.id !== "",
      folders: answers.folders.record_repo !== "" || answers.folders.log_directory !== ""
    }
  };
}

// A contributor id suggested from a display name: "Ada Lovelace" becomes
// "ada-lovelace".
function contributorIdFrom(displayName) {
  return displayName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

// The folders suggested for a new eval, as docs/answers-file.md shows them.
function suggestedFolders(evalName) {
  var name = evalName.trim() || "goodall-eval";
  return { record_repo: "~/evals/" + name + "-records", log_directory: "~/evals/" + name + "-logs.noindex" };
}

// The env file variable suggested for a workstream's API key.
function variableNameFor(workstream) {
  return "ANTHROPIC_API_KEY_" + workstream.toUpperCase().replace(/[^A-Z0-9]+/g, "_");
}

function isName(value) {
  return NAME_PATTERN.test(value);
}

function isFullPath(value) {
  return value.indexOf("/") === 0 || value.indexOf("~/") === 0;
}

function looksLikeAKey(value) {
  for (var index = 0; index < KEY_STARTS.length; index++) {
    if (KEY_STARTS[index].test(value)) {
      return true;
    }
  }
  return false;
}

function looksLikeAReference(value) {
  return value.toLowerCase().indexOf("op://") !== -1;
}

function isWellFormedReference(value) {
  return REFERENCE_PATTERN.test(value);
}

// Why a repository identity cannot be used, or "" when it can. The same
// questions as goodall.eval_config.describe_repository_problem, more briefly.
function repositoryProblem(repository) {
  if (repository.indexOf("://") !== -1 || repository.indexOf("@") !== -1 || repository.indexOf(":") !== -1) {
    return "Write the repository as host/owner/name, such as github.com/your-org/goodall-pilot-app, not as a web or git address.";
  }
  if (repository.indexOf("/") === 0 || repository.indexOf("~") === 0 || repository.indexOf(".") === 0) {
    return "Write the repository's name, such as github.com/your-org/goodall-pilot-app, not a folder on this Mac.";
  }
  if (/\.git$/.test(repository)) {
    return "Leave .git off the end of the repository's name.";
  }
  var parts = repository.split("/");
  if (parts.length < 3 || parts[0].indexOf(".") === -1) {
    return "Write the repository as host/owner/name, such as github.com/your-org/goodall-pilot-app.";
  }
  for (var index = 0; index < parts.length; index++) {
    if (!REPOSITORY_PART_PATTERN.test(parts[index]) || parts[index] === "." || parts[index] === "..") {
      return "A repository's name has no spaces or empty parts: host/owner/name.";
    }
  }
  return "";
}

// The two folders as goodall will use them, for comparing: "~/" stays as
// written, because the page cannot know the home folder, and trailing
// slashes go.
function folderForComparing(folder) {
  return folder.trim().replace(/\/+$/, "");
}

function foldersOverlap(recordRepo, logDirectory) {
  var first = folderForComparing(recordRepo);
  var second = folderForComparing(logDirectory);
  return first === second || second.indexOf(first + "/") === 0 || first.indexOf(second + "/") === 0;
}

// Every problem with the answers, as { place: "what is wrong and what to
// do" }. An empty object means the page will let the person finish.
function problemsIn(state) {
  var problems = {};
  var isNewEval = state.eval === "new";

  if (isNewEval) {
    var newEval = state.new_eval;
    if (!newEval.name.trim()) {
      problems["new_eval.name"] = "Give the eval a short name, such as goodall-pilot.";
    } else if (!isName(newEval.name.trim())) {
      problems["new_eval.name"] = "Use lowercase letters, digits and hyphens only, such as goodall-pilot.";
    }
    if (!newEval.description.trim()) {
      problems["new_eval.description"] = "Say in a sentence what the work is, such as: Building a product to first recurring revenue with AI agents.";
    } else if (EMAIL_ADDRESS_PATTERN.test(newEval.description)) {
      problems["new_eval.description"] = "Take the email address out: the eval config is made to be published.";
    }
    if (newEval.workstreams.length === 0) {
      problems["new_eval.workstreams"] = "Add at least one workstream.";
    } else {
      for (var index = 0; index < newEval.workstreams.length; index++) {
        var workstream = newEval.workstreams[index];
        if (!isName(workstream)) {
          problems["new_eval.workstreams"] = "Workstream names use lowercase letters, digits and hyphens only, such as go-to-market.";
        } else if (workstream === "unknown") {
          problems["new_eval.workstreams"] = "unknown cannot be a workstream: goodall keeps that word for itself.";
        }
      }
    }
    if (!newEval.construct.trim()) {
      problems["new_eval.construct"] = "Write the question this eval sets out to measure. A sentence or two is enough to start.";
    }
    if (newEval.repositories.length === 0) {
      problems["new_eval.repositories"] = "Name at least one repository the work is in, such as github.com/your-org/goodall-pilot-app.";
    } else {
      for (var repositoryIndex = 0; repositoryIndex < newEval.repositories.length; repositoryIndex++) {
        var problem = repositoryProblem(newEval.repositories[repositoryIndex]);
        if (problem) {
          problems["new_eval.repositories"] = problem;
        }
      }
    }
  }

  var contributor = state.contributor;
  if (isNewEval) {
    if (!contributor.display_name.trim()) {
      problems["contributor.display_name"] = "Enter the name that should appear beside your work. Not your email address.";
    } else if (EMAIL_ADDRESS_PATTERN.test(contributor.display_name)) {
      problems["contributor.display_name"] = "Use a name, not an email address: the eval config is made to be published.";
    }
  }
  if (!contributor.id.trim()) {
    problems["contributor.id"] = isNewEval
      ? "Give yourself a short id, such as ada-lovelace."
      : "Enter your id as the eval config's roster has it, such as ada-lovelace.";
  } else if (!isName(contributor.id.trim())) {
    problems["contributor.id"] = "Use lowercase letters, digits and hyphens only, such as ada-lovelace.";
  }
  var workstreams = isNewEval ? state.new_eval.workstreams : state.known_workstreams;
  if (!contributor.main_workstream) {
    problems["contributor.main_workstream"] = "Choose your main workstream.";
  } else if (workstreams.length > 0 && workstreams.indexOf(contributor.main_workstream) === -1) {
    problems["contributor.main_workstream"] = "Choose one of the eval's workstreams: " + workstreams.join(", ") + ".";
  } else if (!isName(contributor.main_workstream)) {
    problems["contributor.main_workstream"] = "Use lowercase letters, digits and hyphens only, such as research.";
  }

  var folders = state.folders;
  var folderNames = { record_repo: "the record repo", log_directory: "raw records" };
  var folderKeys = ["record_repo", "log_directory"];
  for (var folderIndex = 0; folderIndex < folderKeys.length; folderIndex++) {
    var key = folderKeys[folderIndex];
    var folder = folders[key].trim();
    if (!folder) {
      problems["folders." + key] = "Say which folder to use for " + folderNames[key] + ".";
    } else if (!isFullPath(folder)) {
      problems["folders." + key] = "Give the full folder path, starting with / or ~/.";
    }
  }
  if (folders.record_repo.trim() && folders.log_directory.trim() && foldersOverlap(folders.record_repo, folders.log_directory)) {
    problems["folders.log_directory"] = "Use two separate folders, neither inside the other: the record repo is committed with git, and raw records never are.";
  }

  if (state.uses_api_keys) {
    for (var rowIndex = 0; rowIndex < state.secret_references.length; rowIndex++) {
      var row = state.secret_references[rowIndex];
      var place = "secret_references." + rowIndex;
      var reference = row.reference.trim();
      if (!VARIABLE_NAME_PATTERN.test(row.name)) {
        problems[place] = "The variable's name uses letters, digits and underscores only, such as ANTHROPIC_API_KEY_RESEARCH.";
      } else if (row.name === SERVICE_ACCOUNT_TOKEN_VARIABLE) {
        problems[place] = "A 1Password service account token can never be a reference. Leave this one out.";
      } else if (reference && looksLikeAKey(reference)) {
        problems[place] = "This looks like the key itself. Paste its reference (op://vault/item/field) instead, and replace the key with whoever issued it, since it has now been copied.";
      } else if (reference && !isWellFormedReference(reference)) {
        problems[place] = "This is not a 1Password reference. Paste the reference, op://vault/item/field, never the key itself.";
      }
    }
  }

  // A key or a reference in any other box would land in a file that is not
  // the env file, so it is refused wherever it is.
  var texts = textsToSearch(state);
  for (var textIndex = 0; textIndex < texts.length; textIndex++) {
    var text = texts[textIndex];
    if (looksLikeAKey(text.value)) {
      problems[text.place] = "This looks like a key. Keys never go on this page: only references, in the Secrets step.";
    } else if (looksLikeAReference(text.value)) {
      problems[text.place] = "A secret reference belongs in the Secrets step, not here.";
    }
  }
  return problems;
}

// Every box a person types into, other than the secret references.
function textsToSearch(state) {
  var texts = [
    { place: "contributor.id", value: state.contributor.id },
    { place: "folders.record_repo", value: state.folders.record_repo },
    { place: "folders.log_directory", value: state.folders.log_directory }
  ];
  if (state.eval === "new") {
    texts.push({ place: "new_eval.name", value: state.new_eval.name });
    texts.push({ place: "new_eval.description", value: state.new_eval.description });
    texts.push({ place: "new_eval.construct", value: state.new_eval.construct });
    texts.push({ place: "contributor.display_name", value: state.contributor.display_name });
    texts.push({ place: "new_eval.repositories", value: state.new_eval.repositories.join(" ") });
  }
  return texts;
}

// A TOML basic string: in double quotes, with backslashes, quotes and
// control characters escaped.
function tomlString(value) {
  return '"' + escapedForToml(String(value), false) + '"';
}

// A TOML multi-line basic string, for the construct, whose line breaks are
// kept as written so the saved file stays readable.
function tomlMultilineString(value) {
  return '"""\n' + escapedForToml(String(value), true) + '"""';
}

function escapedForToml(text, keepLineBreaks) {
  var escaped = "";
  for (var index = 0; index < text.length; index++) {
    var character = text.charAt(index);
    var code = text.charCodeAt(index);
    if (character === "\\") {
      escaped += "\\\\";
    } else if (character === '"') {
      escaped += '\\"';
    } else if (character === "\n" && keepLineBreaks) {
      escaped += "\n";
    } else if (character === "\n") {
      escaped += "\\n";
    } else if (character === "\t") {
      escaped += "\\t";
    } else if (character === "\r") {
      escaped += "\\r";
    } else if (code < 0x20 || code === 0x7f) {
      escaped += "\\u" + ("0000" + code.toString(16)).slice(-4);
    } else {
      escaped += character;
    }
  }
  return escaped;
}

// A TOML key: bare when it can be, quoted otherwise, so the file always
// parses and goodall can name what is wrong with it.
function tomlKey(name) {
  return VARIABLE_NAME_PATTERN.test(name) ? name : tomlString(name);
}

// The secret references the answers file will hold: rows with a reference
// typed in, when the person said some work blocks use API keys.
function referencesToSave(state) {
  var rows = [];
  if (!state.uses_api_keys) {
    return rows;
  }
  for (var index = 0; index < state.secret_references.length; index++) {
    var row = state.secret_references[index];
    if (row.reference.trim()) {
      rows.push({ name: row.name, reference: row.reference.trim() });
    }
  }
  return rows;
}

// The answers file the page saves: exactly the format docs/answers-file.md
// documents, with the page's one-time code in it.
function answersFileText(state, oneTimeCode) {
  var isNewEval = state.eval === "new";
  var lines = [];
  lines.push("# A goodall answers file, saved by the setup page.");
  lines.push("# It holds no secrets: only references into your secrets manager.");
  lines.push("# To set up from it again without the page: goodall onboard --answers <this file>");
  lines.push("schema_version = " + ANSWERS_SCHEMA_VERSION);
  lines.push("one_time_code = " + tomlString(oneTimeCode));
  lines.push("account_mode = " + tomlString(state.account_mode));
  lines.push("eval = " + tomlString(state.eval));

  if (isNewEval) {
    var newEval = state.new_eval;
    var workstreams = [];
    for (var index = 0; index < newEval.workstreams.length; index++) {
      workstreams.push(tomlString(newEval.workstreams[index]));
    }
    lines.push("");
    lines.push("[new_eval]");
    lines.push("name = " + tomlString(newEval.name.trim()));
    lines.push("description = " + tomlString(newEval.description.trim()));
    lines.push("workstreams = [" + workstreams.join(", ") + "]");
    // ActivityWatch records the whole account all the time, so mixed
    // account mode leaves it out (ADR 0021).
    lines.push("activitywatch = " + (newEval.activitywatch && state.account_mode === "dedicated" ? "true" : "false"));
    if (newEval.construct.trim()) {
      lines.push("construct = " + tomlMultilineString(newEval.construct.trim() + "\n"));
    }
    for (var repositoryIndex = 0; repositoryIndex < newEval.repositories.length; repositoryIndex++) {
      lines.push("");
      lines.push("[[new_eval.scope]]");
      lines.push("repository = " + tomlString(newEval.repositories[repositoryIndex].trim()));
      lines.push('paths = ["**"]');
    }
  }

  lines.push("");
  lines.push("[contributor]");
  lines.push("id = " + tomlString(state.contributor.id.trim()));
  if (isNewEval) {
    lines.push("display_name = " + tomlString(state.contributor.display_name.trim()));
  }
  lines.push("main_workstream = " + tomlString(state.contributor.main_workstream));

  lines.push("");
  lines.push("[folders]");
  lines.push("record_repo = " + tomlString(state.folders.record_repo.trim()));
  lines.push("log_directory = " + tomlString(state.folders.log_directory.trim()));

  var references = referencesToSave(state);
  if (references.length > 0) {
    lines.push("");
    lines.push("[secrets]");
    lines.push('manager = "1password"');
    lines.push("");
    lines.push("[secrets.references]");
    for (var referenceIndex = 0; referenceIndex < references.length; referenceIndex++) {
      var row = references[referenceIndex];
      lines.push(tomlKey(row.name) + " = " + tomlString(row.reference));
    }
  }

  if (state.goodall_program) {
    lines.push("");
    lines.push("[claude_code]");
    lines.push("goodall_program = " + tomlString(state.goodall_program));
  }
  return lines.join("\n") + "\n";
}

// What goodall will set up once it has the answers, for the page's Finish
// step. Goodall's own plan, shown in the terminal, is the one that counts.
function changesGoodallMakes(state, places) {
  var recordRepo = state.folders.record_repo.trim() || "the record repo";
  var changes = [];
  if (state.eval === "new") {
    changes.push([recordRepo, "the record repo: a git repository on this Mac, never pushed anywhere"]);
    changes.push([recordRepo + "/eval.toml", "the eval config: its name, workstreams, scope and you in its roster"]);
    changes.push([recordRepo + "/CONSTRUCT.md", "the construct"]);
  } else {
    changes.push([recordRepo, "used as it is: goodall checks that you are in its roster"]);
  }
  changes.push([state.folders.log_directory.trim() || "the log directory", "the log directory, which only this macOS user account can open"]);
  changes.push([places.env_file, "the env file: secret references only, readable by this account only"]);
  changes.push([places.local_settings, "local settings"]);
  changes.push([places.claude_settings, "goodall's hooks for Claude Code, beside your own settings"]);
  return changes;
}
