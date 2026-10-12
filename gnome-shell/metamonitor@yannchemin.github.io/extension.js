// MetaMonitor top-bar indicator.
//
// Reads the digest the MetaMonitor app writes every second to
// $XDG_RUNTIME_DIR/metamonitor/summary.json and shows the number of active
// tasks with a mini neon meter of their mean completion.  The menu lists
// the active tasks.  Start the app with `metamonitor --background` (or
// enable "Keep running when closed") to keep the digest fresh.
//
// SPDX-License-Identifier: Unlicense

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const APP_ID = 'io.github.yannchemin.MetaMonitor.desktop';
const STALE_SECONDS = 10;
const METER_WIDTH = 42;
const MENU_METER_WIDTH = 420;
const GROUP_CLASS = {
    claude: 'mm-claude', cli: 'mm-cli', grass: 'mm-grass', files: 'mm-files', apps: 'mm-apps',
};

function summaryFile() {
    const spool = GLib.getenv('METAMONITOR_SPOOL');
    const dir = spool
        ? GLib.path_get_dirname(spool)
        : GLib.build_filenamev([GLib.get_user_runtime_dir(), 'metamonitor']);
    return Gio.File.new_for_path(GLib.build_filenamev([dir, 'summary.json']));
}

function meter(fraction, width, styleClass) {
    const track = new St.Widget({
        style_class: `mm-meter-track ${styleClass}`,
        width,
        x_align: Clutter.ActorAlign.START,
        y_align: Clutter.ActorAlign.CENTER,
    });
    const fill = new St.Widget({style_class: 'mm-meter-fill'});
    track.add_child(fill);
    fill.set_width(Math.round(width * Math.max(0, Math.min(1, fraction ?? 0))));
    return {track, fill};
}

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'MetaMonitor');
        this._extension = extension;

        const box = new St.BoxLayout({style_class: 'mm-panel-box'});
        this._icon = new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/metamonitor-symbolic.svg`),
            style_class: 'system-status-icon',
        });
        this._count = new St.Label({
            text: '',
            style_class: 'mm-panel-count',
            y_align: Clutter.ActorAlign.CENTER,
        });
        const {track, fill} = meter(0, METER_WIDTH, 'mm-panel');
        this._fill = fill;
        this._track = track;
        box.add_child(this._icon);
        box.add_child(this._count);
        box.add_child(track);
        this.add_child(box);

        this._section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._section);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Open MetaMonitor', () => this._launch());

        this._summary = null;
        this._file = summaryFile();
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._fillMenu();
        });
    }

    _launch() {
        const app = Gio.DesktopAppInfo.new(APP_ID);
        if (app)
            app.launch([], global.create_app_launch_context(0, -1));
    }

    _refresh() {
        this._file.load_contents_async(null, (file, res) => {
            let summary = null;
            try {
                const [, bytes] = file.load_contents_finish(res);
                summary = JSON.parse(new TextDecoder().decode(bytes));
            } catch {
                summary = null;
            }
            this._summary = summary;
            this._update();
        });
    }

    _update() {
        const s = this._summary;
        const live = s && Date.now() / 1000 - s.updated < STALE_SECONDS;
        this.remove_style_class_name('mm-stale');
        if (!live) {
            this.add_style_class_name('mm-stale');
            this._count.text = '';
            this._track.hide();
            return;
        }
        this._count.text = s.active > 0 ? `${s.active}` : '';
        this._track.visible = s.active > 0;
        this._fill.set_width(Math.round(METER_WIDTH * ((s.mean ?? 0) / 100)));
        this._track.remove_style_class_name('mm-indeterminate');
        if (s.mean === null)
            this._track.add_style_class_name('mm-indeterminate');
        if (this.menu.isOpen)
            this._fillMenu();
    }

    _fillMenu() {
        this._section.removeAll();
        const s = this._summary;
        if (!s || Date.now() / 1000 - s.updated >= STALE_SECONDS) {
            this._section.addMenuItem(new PopupMenu.PopupMenuItem(
                'MetaMonitor is not running', {reactive: false}));
            return;
        }
        if (!s.tasks.length) {
            this._section.addMenuItem(new PopupMenu.PopupMenuItem(
                'Nothing in progress', {reactive: false}));
            return;
        }
        for (const t of s.tasks) {
            const item = new PopupMenu.PopupBaseMenuItem({style_class: 'mm-task-item'});
            const column = new St.BoxLayout({vertical: true, x_expand: true});
            const top = new St.BoxLayout({x_expand: true});
            const title = new St.Label({
                text: t.title,
                x_expand: true,
                style_class: t.child ? 'mm-task-title mm-child' : 'mm-task-title',
            });
            title.clutter_text.ellipsize = 3;  // Pango.EllipsizeMode.END
            const pct = new St.Label({
                text: t.percent === null ? t.state.toUpperCase() : `${t.percent.toFixed(0)}%`,
                style_class: 'mm-task-pct',
            });
            top.add_child(title);
            top.add_child(pct);
            column.add_child(top);
            const {track} = meter(t.percent === null ? 1 : t.percent / 100, MENU_METER_WIDTH,
                `${GROUP_CLASS[t.group] ?? 'mm-cli'} ${t.percent === null ? 'mm-indeterminate' : ''}`);
            column.add_child(track);
            item.add_child(column);
            item.connect('activate', () => this._launch());
            this._section.addMenuItem(item);
        }
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        super.destroy();
    }
});

export default class MetaMonitorExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
