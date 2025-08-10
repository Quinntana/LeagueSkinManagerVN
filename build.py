import os
import shutil
import PyInstaller.__main__

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, 'src')
DIST_DIR = os.path.join(PROJECT_ROOT, 'dist')
BUILD_DIR = os.path.join(PROJECT_ROOT, 'build_main')
BUILD_DIR_UNINSTALL = os.path.join(PROJECT_ROOT, 'build_uninstall')

MAIN_NAME = 'LeagueSkinManagerVN'
UNINSTALL_NAME = 'LeagueSkinManagerVNUninstall'

COMMON_ARGS = [
    '--onefile',
    '--distpath', DIST_DIR,
    '--paths', SRC_DIR,
    '--add-data', f'{SRC_DIR}{os.pathsep}.', 
    '--hidden-import=config',
    '--hidden-import=logger',
    '--hidden-import=champions',
    '--hidden-import=skin_downloader',
    '--hidden-import=skin_installer',
    '--hidden-import=update_checker',
]

def clean():
    for p in (BUILD_DIR, BUILD_DIR_UNINSTALL, DIST_DIR):
        if os.path.exists(p):
            shutil.rmtree(p)

def build_main():
    args = COMMON_ARGS + [
        '--workpath', BUILD_DIR,
        '--name', MAIN_NAME,
        '--noconsole', 
        os.path.join(SRC_DIR, 'main.py'),
    ]
    print("Building main:", ' '.join(args))
    PyInstaller.__main__.run(args)

def build_uninstall():
    args = COMMON_ARGS + [
        '--workpath', BUILD_DIR_UNINSTALL,
        '--name', UNINSTALL_NAME,
        '--noconsole',    
        '--uac-admin',   
        os.path.join(SRC_DIR, 'uninstall.py'),
    ]
    print("Building uninstall:", ' '.join(args))
    PyInstaller.__main__.run(args)

def main():
    clean()
    build_main()
    build_uninstall()
    print("Builds complete — check the dist/ folder for the exes")

if __name__ == '__main__':
    main()