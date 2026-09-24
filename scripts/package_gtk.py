"""Companion package templates: distinct files, exact core dependency, no activation."""

RPM_SPEC = '''Name: oscmix-desk-gtk
Version: @VERSION@
Release: @RELEASE@
Summary: Optional upstream GTK mixer for oscmix-desk
License: MIT AND ISC AND Unlicense
URL: https://github.com/relative23/oscmix-desk
Source0: payload.tar
Requires: oscmix-desk%{?_isa} = @VERSION@-@RELEASE@
Requires(pre): /usr/bin/python3
Requires(post): /usr/bin/glib-compile-schemas
Requires(postun): /usr/bin/glib-compile-schemas
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}
%global _build_id_links none

%description
The pinned upstream GTK mixer, desktop entry and schema. Uses the
exactly matching oscmix-desk core backend. Installing starts no mixer.

%prep
%build
%install
mkdir -p %{buildroot}
tar -xf %{SOURCE0} -C %{buildroot}

%pre
/usr/bin/python3 -I <<'PYTHON_GUARD'
@INSTALL_GUARD@
PYTHON_GUARD

%post
glib-compile-schemas /usr/share/glib-2.0/schemas || exit 1

%preun
if [ "$1" -eq 0 ]; then
/usr/bin/python3 -I <<'PYTHON_GUARD'
@REMOVE_GUARD@
PYTHON_GUARD
fi

%postun
glib-compile-schemas /usr/share/glib-2.0/schemas || exit 1
if [ "$1" -eq 0 ]; then
/usr/bin/python3 -I <<'PYTHON_GUARD'
@FINISH_GUARD@
PYTHON_GUARD
fi

%posttrans
/usr/lib/oscmix-desk/package-guard finish --component gtk

%files
%defattr(-,root,root)
/usr/bin/oscmix-gtk
/usr/share/oscmix-desk/gtk-package.json
/usr/share/applications/oscmix-gtk.desktop
/usr/share/icons/hicolor/scalable/apps/oscmix.svg
/usr/share/glib-2.0/schemas/oscmix.gschema.xml
%license /usr/share/licenses/oscmix-desk-gtk
'''

ARCH_INSTALL = '''#!/bin/bash
post_install() {
    glib-compile-schemas /usr/share/glib-2.0/schemas || return 1
    /usr/lib/oscmix-desk/package-guard finish --component gtk || return 1
}
post_upgrade() { post_install; }
post_remove() {
    glib-compile-schemas /usr/share/glib-2.0/schemas || return 1
    /usr/bin/python3 -I <<'PYTHON_GUARD'
@FINISH_GUARD@
PYTHON_GUARD
}
'''

ARCH_PREPARE = '''
_oscmix_prepare() {
    /usr/bin/python3 -I <<'PYTHON_GUARD'
@INSTALL_GUARD@
PYTHON_GUARD
}
pre_install() { _oscmix_prepare; }
pre_upgrade() { _oscmix_prepare; }
pre_remove() {
    /usr/bin/python3 -I <<'PYTHON_GUARD'
@REMOVE_GUARD@
PYTHON_GUARD
}
'''
