"""Command line interface for the local project/deadline store."""
import argparse
from project_store import ProjectStore


class DeadlineCLI:
    def __init__(self, project_store: ProjectStore):
        self.project_store = project_store
        self.parser = argparse.ArgumentParser(description="Manage JARVIS projects and deadlines")
        commands = self.parser.add_subparsers(dest="command")
        p = commands.add_parser("project-add", help="Register a project")
        p.add_argument("name"); p.add_argument("path")
        p = commands.add_parser("project-list", help="List projects")
        p = commands.add_parser("add", help="Add a deadline")
        p.add_argument("project", help="Project name or registered path"); p.add_argument("title"); p.add_argument("due_date", help="ISO date/time, e.g. 2026-11-07 or 2026-11-07T09:00")
        p = commands.add_parser("update", help="Change a deadline status")
        p.add_argument("id", type=int); p.add_argument("status", choices=["open", "completed", "cancelled"])
        p = commands.add_parser("list", help="List deadlines")
        p.add_argument("--project"); p.add_argument("--all", action="store_true")
        commands.add_parser("due-soon", help="List open deadlines due in the next 48 hours")

    def run(self, argv: list[str]) -> str:
        args = self.parser.parse_args(argv)
        if args.command == "project-add":
            project_id = self.project_store.add_project(args.name, args.path)
            return f"Project {args.name} registered (ID {project_id})."
        if args.command == "project-list":
            rows = self.project_store.list_projects()
            return "\n".join(f"{p['id']}: {p['name']} — {p['path']}" for p in rows) or "No projects registered."
        if args.command == "add":
            projects = self.project_store.list_projects()
            project = next((p for p in projects if p["name"].casefold() == args.project.casefold() or p["path"].casefold() == args.project.casefold()), None)
            if project is None:
                raise ValueError(f"Unknown project {args.project!r}; register it with project-add first.")
            deadline_id = self.project_store.add_deadline(project["id"], args.title, args.due_date)
            return f"Deadline {deadline_id} added for {project['name']}: {args.title}, due {args.due_date}."
        if args.command == "update":
            return f"Deadline {args.id} updated." if self.project_store.update_deadline_status(args.id, args.status) else f"Deadline {args.id} not found."
        if args.command == "due-soon":
            rows = self.project_store.get_proactive_deadlines()
        elif args.command == "list":
            project_id = None
            if args.project:
                p = next((x for x in self.project_store.list_projects() if x["name"].casefold() == args.project.casefold() or x["path"].casefold() == args.project.casefold()), None)
                if not p: return f"Unknown project {args.project!r}."
                project_id = p["id"]
            rows = self.project_store.list_deadlines(project_id, "all" if args.all else "open")
        else:
            self.parser.print_help(); return ""
        if not rows: return "No matching deadlines."
        lines = []
        for d in rows:
            project = self.project_store.get_project(d["project_id"])
            lines.append(f"{d['id']}: {d['title']} (due {d['due_date']}; {project['name'] if project else 'Unknown'}; {d['status']})")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    store = ProjectStore(Path(__file__).resolve().with_name("jarvis_projects.db"))
    cli = DeadlineCLI(store)
    print(cli.run(sys.argv[1:]))
