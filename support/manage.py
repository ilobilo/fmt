#!/usr/bin/env python

"""Manage site and releases.

Usage:
  manage.py release [<branch>]
  manage.py site
"""

from __future__ import print_function
import datetime, docopt, fileinput, json, os
import re, requests, shutil, sys, tempfile
from distutils.version import LooseVersion
from subprocess import check_call


class Git:
    def __init__(self, dir):
        self.dir = dir

    def call(self, method, args, **kwargs):
        return check_call(['git', method] + list(args), **kwargs)

    def clone(self, *args):
        return self.call('clone', list(args) + [self.dir])

    def checkout(self, *args):
        return self.call('checkout', args, cwd=self.dir)

    def clean(self, *args):
        return self.call('clean', args, cwd=self.dir)

    def reset(self, *args):
        return self.call('reset', args, cwd=self.dir)

    def pull(self, *args):
        return self.call('pull', args, cwd=self.dir)

    def update(self, *args):
        if not os.path.exists(self.dir):
            self.clone(*args)


class Runner:
    def __init__(self):
        self.cwd = '.'

    def __call__(self, *args, **kwargs):
        kwargs['cwd'] = kwargs.get('cwd', self.cwd)
        check_call(args, **kwargs)


def create_build_env():
    """Create a build environment."""
    class Env:
        pass
    env = Env()

    # Import the documentation build module.
    env.fmt_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(env.fmt_dir, 'doc'))
    import build

    env.build_dir = 'build'

    # Virtualenv and repos are cached to speed up builds.
    build.create_build_env(os.path.join(env.build_dir, 'virtualenv'))

    env.fmt_repo = Git(os.path.join(env.build_dir, 'fmt'))
    return env


fmt_repo_url = 'git@github.com:fmtlib/fmt'


def update_site(env):
    env.fmt_repo.update(fmt_repo_url)

    doc_repo = Git(os.path.join(env.build_dir, 'fmtlib.github.io'))
    doc_repo.update('git@github.com:fmtlib/fmtlib.github.io')

    for version in ['1.0.0', '1.1.0', '2.0.0', '3.0.0']:
        env.fmt_repo.clean('-f', '-d')
        env.fmt_repo.reset('--hard')
        env.fmt_repo.checkout(version)
        target_doc_dir = os.path.join(env.fmt_repo.dir, 'doc')
        # Remove the old theme.
        for entry in os.listdir(target_doc_dir):
            path = os.path.join(target_doc_dir, entry)
            if os.path.isdir(path):
                shutil.rmtree(path)
        # Copy the new theme.
        for entry in ['_static', '_templates', 'basic-bootstrap', 'bootstrap',
                      'conf.py', 'fmt.less']:
            src = os.path.join(env.fmt_dir, 'doc', entry)
            dst = os.path.join(target_doc_dir, entry)
            copy = shutil.copytree if os.path.isdir(src) else shutil.copyfile
            copy(src, dst)
        # Rename index to contents.
        contents = os.path.join(target_doc_dir, 'contents.rst')
        if not os.path.exists(contents):
            os.rename(os.path.join(target_doc_dir, 'index.rst'), contents)
        # Fix issues in reference.rst/api.rst.
        for filename in ['reference.rst', 'api.rst']:
            reference = os.path.join(target_doc_dir, filename)
            if not os.path.exists(reference):
                continue
            with open(reference) as f:
                data = f.read()
            data = data.replace('std::ostream &', 'std::ostream&')
            pattern = re.compile('doxygenfunction.. (bin|oct|hexu|hex)$', re.M)
            data = re.sub(pattern, r'doxygenfunction:: \1(int)', data)
            data = data.replace('std::FILE*', 'std::FILE *')
            data = data.replace('unsigned int', 'unsigned')
            with open(reference, 'w') as f:
                f.write(data)
        # Build the docs.
        html_dir = os.path.join(env.build_dir, 'html')
        if os.path.exists(html_dir):
            shutil.rmtree(html_dir)
        include_dir = env.fmt_repo.dir
        if LooseVersion(version) >= LooseVersion('3.0.0'):
            include_dir = os.path.join(include_dir, 'fmt')
        import build
        build.build_docs(version, doc_dir=target_doc_dir,
                         include_dir=include_dir, work_dir=env.build_dir)
        shutil.rmtree(os.path.join(html_dir, '.doctrees'))
        # Create symlinks for older versions.
        for link, target in {'index': 'contents', 'api': 'reference'}.items():
            link = os.path.join(html_dir, link) + '.html'
            target += '.html'
            if os.path.exists(os.path.join(html_dir, target)) and \
               not os.path.exists(link):
                os.symlink(target, link)
        # Copy docs to the website.
        version_doc_dir = os.path.join(doc_repo.dir, version)
        shutil.rmtree(version_doc_dir)
        shutil.move(html_dir, version_doc_dir)


def release(args):
    env = create_build_env()

    branch = args.get('<branch>')
    if branch is None:
        branch = 'master'
    env.fmt_repo.update('-b', branch, fmt_repo_url)

    # Convert changelog from RST to GitHub-flavored Markdown and get the
    # version.
    changelog = 'ChangeLog.rst'
    changelog_path = os.path.join(env.fmt_repo.dir, changelog)
    import rst2md
    changes, version = rst2md.convert(changelog_path)
    cmakelists = 'CMakeLists.txt'
    for line in fileinput.input(os.path.join(env.fmt_repo.dir, cmakelists),
                                inplace=True):
        prefix = 'set(FMT_VERSION '
        if line.startswith(prefix):
            line = prefix + version + ')\n'
        sys.stdout.write(line)

    # Update the version in the changelog.
    title_len = 0
    for line in fileinput.input(changelog_path, inplace=True):
        if line.decode('utf-8').startswith(version + ' - TBD'):
            line = version + ' - ' + datetime.date.today().isoformat()
            title_len = len(line)
            line += '\n'
        elif title_len:
            line = '-' * title_len + '\n'
            title_len = 0
        sys.stdout.write(line)
    run = Runner()
    run.cwd = env.fmt_repo.dir
    run('git', 'checkout', '-b', 'release')
    run('git', 'add', changelog, cmakelists)
    run('git', 'commit', '-m', 'Update version')

    # Build the docs and package.
    run('cmake', '.')
    run('make', 'doc', 'package_source')

    update_site(env)

    # Create a release on GitHub.
    run('git', 'push', 'origin', 'release', cwd=env.fmt_repo.dir)
    r = requests.post('https://api.github.com/repos/fmtlib/fmt/releases',
                      params={'access_token': os.getenv('FMT_TOKEN')},
                      data=json.dumps({'tag_name': version,
                                       'target_commitish': 'release',
                                       'body': changes, 'draft': True}))
    if r.status_code != 201:
        raise Exception('Failed to create a release ' + str(r))


if __name__ == '__main__':
    args = docopt.docopt(__doc__)
    if args.get('release'):
        release(args)
    elif args.get('site'):
        update_site(create_build_env())
