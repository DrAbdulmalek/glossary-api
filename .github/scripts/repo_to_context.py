#!/usr/bin/env python3
"""
repo_to_context.py
================================================================================
سكريبت استخراج السياق الذكي من المستودعات (Smart Context Builder)
================================================================================

الوصف:
    يقوم بقراءة مشروع برمجي بالكامل وتحويله إلى ملف Markdown/TXT منظم
    جاهز للرفع إلى نماذج اللغة الكبيرة (LLMs) مثل Gemini Flash.

المميزات:
    - استكشاف شجرة الملفات (File Tree)
    - تصفية ذكية للملفات الثنائية والكبيرة
    - استخراج الـ Dependencies والـ Imports
    - تقسيم تلقائي (Chunking) حسب حدود التوكنز
    - دعم اللغة العربية (UTF-8)

الاستخدام:
    python repo_to_context.py /path/to/your/project --output context.md
    python repo_to_context.py /path/to/your/project --max-size 500000 --split

المخرجات:
    - ملف Markdown واحد (أو عدة ملفات إذا استُخدم --split)
    - يحتوي على كل الكود + الخريطة + الإحصائيات
"""

import os
import sys
import re
import argparse
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ============================================================================
# الإعدادات الافتراضية
# ============================================================================

DEFAULT_MAX_FILE_SIZE = 100 * 1024  # 100 KB (الحد الأقصى لحجم الملف الواحد)
DEFAULT_MAX_TOTAL_SIZE = 800_000    # 800K حرف (حجم آمن لـ Gemini Flash)

# الملفات والامتدادات التي نستبعدها
EXCLUDED_DIRS = {
    '.git', '.github', '.vscode', '__pycache__', 'node_modules',
    'venv', 'env', '.venv', 'dist', 'build', '.pytest_cache',
    '.mypy_cache', '.idea', '.DS_Store', 'coverage', 'htmlcov',
    '.tox', '.eggs', '*.egg-info', 'site-packages', 'lib',
    'bin', 'include', 'share', 'docs/_build', 'target'
}

EXCLUDED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar', '.xz',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.db',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.pyc', '.pyo', '.class', '.o', '.obj', '.a', '.lib'
}

EXCLUDED_FILES = {
    # ملاحظة: dotfiles تُعالج في should_include عبر ALLOWED_DOTFILES
    'LICENSE', 'LICENSE.txt', 'COPYING', 'AUTHORS', 'CHANGELOG',
    'MANIFEST.in', 'setup.cfg', 'tox.ini',
    'poetry.lock', 'Pipfile.lock', 'package-lock.json', 'yarn.lock',
    'pnpm-lock.yaml', 'Cargo.lock', 'Gemfile.lock',
    # ملفات الأسرار الشائعة (لا تُرفع EVER)
    'secrets.json', 'secrets.yaml', 'secrets.yml', 'secrets.toml',
    'credentials.json', 'service-account.json',
    '.npmrc', '.pypirc', '.netrc',  # قد تحتوي tokens
    'id_rsa', 'id_ecdsa', 'id_ed25519',  # SSH private keys
    '.htpasswd', '.aws/credentials',
}

# امتدادات إضافية تُستبعد (سرية محتملة)
EXCLUDED_EXTENSIONS |= {
    '.pem', '.key', '.p12', '.pfx',  # شهادات ومفاتيح خاصة
    '.token',  # ملفات tokens
}

# أنماط أسماء ملفات تُستبعد (regex على الاسم الكامل)
# الهدف: حماية شاملة من تسريب الأسرار بكل صيغ التسمية الشائعة
EXCLUDED_PATTERNS = [
    # .env.*  ما عدا .env.example (template آمن)
    r'^\.env\.(?!example$).+$',
    # أي ملف يحتوي كلمات أسرار بصيغة مرنة (مفرد/جمع/مع/بدون underscores)
    # أمثلة على ما يلتقطه: secrets.json, api_keys.yaml, my_credentials.yml,
    # service_token.txt, password_file.ini, private_key.cfg
    r'(?i).*(secrets?|credentials?|tokens?|api[_-]?keys?|private[_-]?keys?|passwords?)[a-z0-9_-]*\.(json|yaml|yml|toml|ini|env|txt|cfg|conf|properties)$',
    # SSH key files (بدون امتداد)
    r'^id_(rsa|ecdsa|ed25519|dsa)$',
    # Google service account files
    r'.*service[_-]?account.*\.json$',
]

# أولوية الملفات (تظهر أولاً في السياق)
HIGH_PRIORITY_FILES = [
    'README', 'README.md', 'README.txt', 'README.rst',
    'CRITICAL_KEYWORDS.py', 'medical_doc_gui', 'main.py', 'app.py',
    'requirements.txt', 'setup.py', 'pyproject.toml', 'config.py',
    'settings.py', 'constants.py', 'models.py', 'views.py',
    'controller.py', 'utils.py', 'helpers.py'
]

# ============================================================================
# دوال المساعدة
# ============================================================================

def is_text_file(file_path: Path) -> bool:
    """التحقق مما إذا كان الملف نصياً (وليس ثنائياً)."""
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(1024)
            if b'\x00' in chunk:
                return False
            # محاولة فك التشفير كـ UTF-8
            chunk.decode('utf-8')
        return True
    except (UnicodeDecodeError, OSError):
        return False


# ملفات dotfile آمنة يُسمح بتضمينها (رغم أنها تبدأ بـ '.')
ALLOWED_DOTFILES = {
    '.env.example',  # template آمن للمرجعية
    '.gitignore',    # مفيد لفهم ما يُستبعد
    '.dockerignore',
    '.python-version',
    '.nvmrc',
    '.ruby-version',
}


def should_include(file_path: Path, root: Path) -> bool:
    """تحديد ما إذا كان يجب تضمين الملف في السياق."""
    # استبعاد المجلدات
    for part in file_path.parts:
        if part in EXCLUDED_DIRS:
            return False
        # استبعاد dotfiles ما عدا المسموح بها
        if part.startswith('.') and part not in ALLOWED_DOTFILES:
            return False

    # استبعاد الامتدادات
    if file_path.suffix.lower() in EXCLUDED_EXTENSIONS:
        return False

    # استبعاد الملفات المحددة
    if file_path.name in EXCLUDED_FILES:
        return False

    # استبعاد الأنماط (regex) — حماية من تسريب الأسرار
    for pattern in EXCLUDED_PATTERNS:
        if re.search(pattern, file_path.name):
            return False

    # التحقق من الحجم
    try:
        if file_path.stat().st_size > DEFAULT_MAX_FILE_SIZE:
            return False
    except OSError:
        return False

    # التحقق من أنه ملف نصي
    if not is_text_file(file_path):
        return False

    return True


def get_file_priority(file_path: Path) -> int:
    """تحديد أولوية الملف (0 = أعلى أولوية)."""
    name = file_path.name
    stem = file_path.stem

    for i, priority_name in enumerate(HIGH_PRIORITY_FILES):
        if name == priority_name or stem == priority_name:
            return i

    # أولوية أقل للملفات الكبيرة جداً
    try:
        size = file_path.stat().st_size
        if size > 50 * 1024:
            return 1000
    except OSError:
        pass

    return 500


def extract_imports(content: str, file_ext: str) -> list:
    """استخراج الـ Imports من الكود."""
    imports = []
    lines = content.split('\n')

    for line in lines[:50]:  # نفحص أول 50 سطر فقط
        line = line.strip()
        if file_ext == '.py':
            if line.startswith('import ') or line.startswith('from '):
                imports.append(line)
        elif file_ext in ('.js', '.ts'):
            if line.startswith('import ') or line.startswith('require('):
                imports.append(line)
        elif file_ext in ('.c', '.cpp', '.h'):
            if line.startswith('#include'):
                imports.append(line)

    return imports


def build_file_tree(root: Path, included_files: list) -> str:
    """بناء شجرة الملفات النصية."""
    tree_lines = []
    tree_lines.append(f"{root.name}/")

    # بناء شجرة نسبية
    relative_files = [f.relative_to(root) for f in included_files]
    relative_files.sort()

    # بناء الشجرة
    dirs_seen = set()
    for file_path in relative_files:
        parts = list(file_path.parts)
        prefix = ""
        for i, part in enumerate(parts[:-1]):
            dir_path = tuple(parts[:i+1])
            if dir_path not in dirs_seen:
                tree_lines.append(f"{'    ' * i}├── {part}/")
                dirs_seen.add(dir_path)

        depth = len(parts) - 1
        tree_lines.append(f"{'    ' * depth}├── {parts[-1]}")

    return "\n".join(tree_lines)


def build_context(project_path: str, max_total_size: int = DEFAULT_MAX_TOTAL_SIZE) -> dict:
    """
    بناء السياق الكامل للمشروع.

    Returns:
        dict يحتوي على: metadata, file_tree, files_content, stats
    """
    root = Path(project_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"المسار غير موجود: {project_path}")

    # جمع الملفات
    all_files = []
    for file_path in root.rglob('*'):
        if file_path.is_file() and should_include(file_path, root):
            all_files.append(file_path)

    # ترتيب حسب الأولوية
    all_files.sort(key=get_file_priority)

    # إحصائيات
    stats = {
        'total_files_scanned': len(list(root.rglob('*'))),
        'included_files': len(all_files),
        'total_size_bytes': sum(f.stat().st_size for f in all_files),
        'languages': defaultdict(int),
        'extensions': defaultdict(int)
    }

    # قراءة المحتوى
    files_data = []
    for file_path in all_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            rel_path = file_path.relative_to(root)
            ext = file_path.suffix.lower()

            stats['extensions'][ext or 'no_extension'] += 1

            # تحديد اللغة
            lang_map = {
                '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
                '.html': 'HTML', '.css': 'CSS', '.java': 'Java',
                '.c': 'C', '.cpp': 'C++', '.h': 'C/C++ Header',
                '.go': 'Go', '.rs': 'Rust', '.rb': 'Ruby',
                '.php': 'PHP', '.swift': 'Swift', '.kt': 'Kotlin',
                '.md': 'Markdown', '.txt': 'Text', '.json': 'JSON',
                '.xml': 'XML', '.yaml': 'YAML', '.yml': 'YAML',
                '.sql': 'SQL', '.sh': 'Shell', '.bat': 'Batch'
            }
            lang = lang_map.get(ext, 'Other')
            stats['languages'][lang] += 1

            # استخراج الـ imports
            imports = extract_imports(content, ext)

            files_data.append({
                'path': str(rel_path),
                'language': lang,
                'size': len(content),
                'lines': content.count('\n') + 1,
                'imports': imports,
                'content': content
            })

        except Exception as e:
            print(f"[WARNING] فشل قراءة {file_path}: {e}")

    # بناء شجرة الملفات
    file_tree = build_file_tree(root, all_files)

    return {
        'project_name': root.name,
        'project_path': str(root),
        'generated_at': datetime.now().isoformat(),
        'stats': dict(stats),
        'file_tree': file_tree,
        'files': files_data
    }


def format_as_markdown(context: dict, include_content: bool = True) -> str:
    """تنسيق السياق كـ Markdown."""
    lines = []

    # العنوان
    lines.append(f"# سياق المشروع: {context['project_name']}")
    lines.append(f"**المسار:** `{context['project_path']}`")
    lines.append(f"**تاريخ التوليد:** {context['generated_at']}")
    lines.append("")

    # الإحصائيات
    lines.append("## 📊 إحصائيات المشروع")
    stats = context['stats']
    lines.append(f"- **إجمالي الملفات الممسوحة:** {stats['total_files_scanned']}")
    lines.append(f"- **الملفات المضمنة:** {stats['included_files']}")
    lines.append(f"- **إجمالي الحجم:** {stats['total_size_bytes']:,} بايت")
    lines.append("")

    # اللغات
    lines.append("### اللغات والتقنيات:")
    for lang, count in sorted(stats['languages'].items(), key=lambda x: -x[1]):
        lines.append(f"- {lang}: {count} ملف")
    lines.append("")

    # شجرة الملفات
    lines.append("## 🌳 شجرة الملفات")
    lines.append("```")
    lines.append(context['file_tree'])
    lines.append("```")
    lines.append("")

    if not include_content:
        return "\n".join(lines)

    # محتوى الملفات
    lines.append("## 📁 محتوى الملفات")
    lines.append("")

    for file_data in context['files']:
        lines.append(f"### `{file_data['path']}`")
        lines.append(f"**اللغة:** {file_data['language']} | **الأسطر:** {file_data['lines']} | **الحجم:** {file_data['size']:,} حرف")

        if file_data['imports']:
            lines.append("**الاعتماديات:**")
            for imp in file_data['imports'][:10]:  # أول 10 فقط
                lines.append(f"- `{imp}`")

        lines.append("")
        lines.append(f"```{file_data['language'].lower()}")
        lines.append(file_data['content'])
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def split_context(context: dict, max_chunk_size: int = DEFAULT_MAX_TOTAL_SIZE) -> list:
    """
    تقسيم السياق إلى أجزاء (Chunks) إذا كان كبيراً جداً.

    Returns:
        قائمة من السلاسل النصية (كل جزء جاهز للرفع لـ Gemini)
    """
    full_md = format_as_markdown(context, include_content=True)

    if len(full_md) <= max_chunk_size:
        return [full_md]

    # تقسيم حسب الملفات
    chunks = []
    current_chunk = []
    current_size = 0

    # الجزء الأول يحتوي على المقدمة والشجرة
    header = format_as_markdown(context, include_content=False)
    header_size = len(header)

    for file_data in context['files']:
        file_md = f"### `{file_data['path']}`\n"
        file_md += f"**اللغة:** {file_data['language']} | **الأسطر:** {file_data['lines']}\n\n"
        file_md += f"```{file_data['language'].lower()}\n"
        file_md += file_data['content']
        file_md += "\n```\n\n---\n\n"

        file_size = len(file_md)

        # إذا كان الملف وحده أكبر من الحد، نستبعده (نادر)
        if file_size > max_chunk_size:
            print(f"[WARNING] ملف كبير جداً تم تخطيه: {file_data['path']} ({file_size:,} حرف)")
            continue

        # إذا امتلأ الجزء الحالي، نبدأ جزءاً جديداً
        if current_size + file_size + header_size > max_chunk_size and current_chunk:
            chunk_text = header + "\n## 📁 محتوى الملفات (الجزء {})\n\n".format(len(chunks) + 1)
            chunk_text += "\n".join(current_chunk)
            chunks.append(chunk_text)
            current_chunk = []
            current_size = 0

        current_chunk.append(file_md)
        current_size += file_size

    # إضافة الجزء الأخير
    if current_chunk:
        chunk_text = header + "\n## 📁 محتوى الملفات (الجزء {})\n\n".format(len(chunks) + 1)
        chunk_text += "\n".join(current_chunk)
        chunks.append(chunk_text)

    return chunks


# ============================================================================
# الدالة الرئيسية
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="استخراج السياق الذكي من المستودعات لنماذج اللغة الكبيرة",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
أمثلة الاستخدام:
    # استخراج السياق كاملًا
    python repo_to_context.py /path/to/project --output context.md

    # تقسيم تلقائي إذا كان المشروع كبيراً
    python repo_to_context.py /path/to/project --output context --split

    # تخصيص الحد الأقصى
    python repo_to_context.py /path/to/project --max-size 200000 --output context.md
        """
    )

    parser.add_argument('project_path', help='مسار المشروع (المجلد الجذر)')
    parser.add_argument('--output', '-o', default='project_context',
                        help='اسم ملف المخرج (بدون الامتداد)')
    parser.add_argument('--max-size', '-m', type=int, default=DEFAULT_MAX_TOTAL_SIZE,
                        help=f'الحد الأقصى لحجم السياق بالأحرف (افتراضي: {DEFAULT_MAX_TOTAL_SIZE:,})')
    parser.add_argument('--split', '-s', action='store_true',
                        help='تقسيم السياق إلى أجزاء إذا كان كبيراً')
    parser.add_argument('--json', '-j', action='store_true',
                        help='تصدير كـ JSON بدلاً من Markdown')

    args = parser.parse_args()

    print(f"🔍 جارٍ استكشاف المشروع: {args.project_path}")

    try:
        context = build_context(args.project_path, args.max_size)

        print(f"✅ تم العثور على {context['stats']['included_files']} ملفًا")
        print(f"📊 إجمالي الحجم: {context['stats']['total_size_bytes']:,} بايت")
        print(f"🌐 اللغات: {', '.join(context['stats']['languages'].keys())}")

        if args.json:
            # تصدير JSON
            output_file = f"{args.output}.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(context, f, ensure_ascii=False, indent=2)
            print(f"💾 تم حفظ JSON: {output_file}")

        elif args.split:
            # تقسيم إلى أجزاء
            chunks = split_context(context, args.max_size)
            print(f"📦 تم تقسيم السياق إلى {len(chunks)} جزءًا")

            for i, chunk in enumerate(chunks, 1):
                chunk_file = f"{args.output}_part{i:02d}.md"
                with open(chunk_file, 'w', encoding='utf-8') as f:
                    f.write(chunk)
                print(f"   💾 الجزء {i}: {chunk_file} ({len(chunk):,} حرف)")

        else:
            # ملف واحد
            md_content = format_as_markdown(context)
            output_file = f"{args.output}.md"

            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(md_content)

            print(f"💾 تم حفظ Markdown: {output_file} ({len(md_content):,} حرف)")

            if len(md_content) > args.max_size:
                print(f"⚠️ تحذير: حجم الملف ({len(md_content):,}) يتجاوز الحد الأقصى ({args.max_size:,})")
                print(f"   استخدم --split لتقسيمه، أو --max-size لزيادة الحد")

    except Exception as e:
        print(f"❌ خطأ: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
