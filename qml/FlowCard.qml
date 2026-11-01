import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: card
    required property var backend
    required property var theme
    property string title: ""
    property string summary: ""
    property string modelName: ""
    property string badge: ""
    property color badgeColor: theme.accent
    property string timingKey: ""
    property bool checkable: false
    property bool checked: true
    property bool configurable: true
    property string settingsText: "Settings"
    property alias actions: footer.data
    property var stats: (theme.stepTimings || backend.stageTimings)[timingKey] || ({})
    signal configure()
    signal toggled(bool value)
    function duration(seconds) {
        if (seconds > 0 && seconds < .001) return "<1 ms"
        if (seconds < 1) return Math.round(seconds * 1000) + " ms"
        if (seconds < 60) return seconds.toFixed(1) + " s"
        return Math.floor(seconds / 60) + "m " + Math.round(seconds % 60) + "s"
    }
    implicitWidth: 220; implicitHeight: Math.max(218, cardBody.implicitHeight + 22)
    radius: 5; color: theme.panel; border.color: stats.active ? theme.accent : theme.line
    opacity: !checkable || checked ? 1 : .65
    MouseArea { anchors.fill: parent; enabled: !card.checkable && card.configurable; onClicked: card.configure() }
    ColumnLayout {
        id: cardBody; anchors.fill: parent; anchors.margins: 11; spacing: 6
        RowLayout {
            Layout.fillWidth: true
            Rectangle {
                implicitWidth: 24; implicitHeight: 24; radius: 12
                color: !card.checkable || card.checked ? card.badgeColor : theme.line
                Label { anchors.centerIn: parent; text: card.badge; color: theme.bg; font.bold: true; font.pixelSize: 11 }
            }
            Label { text: card.title; color: theme.ink; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            CheckBox { visible: card.checkable; checked: card.checked; onToggled: card.toggled(checked) }
        }
        Label { visible: Boolean(card.summary); text: card.summary; color: theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true }
        Label { visible: Boolean(card.modelName); text: "Model: " + card.modelName; color: theme.ink; font.pixelSize: 11; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
        Label {
            text: (card.checkable ? (card.checked ? "Enabled" : "Off") : "Process step") + (card.stats.cached ? " · cache " + card.stats.cached + "×" : "")
            color: theme.muted; font.pixelSize: 12
        }
        Label {
            text: card.stats.count ? "Average " + card.duration(card.stats.average) + " · " + card.stats.count + " calls" : "Average — · no completed calls"
            color: theme.ink; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap
        }
        Label {
            text: card.stats.active ? "Running · " + card.duration(card.stats.elapsed || 0) + (card.stats.active > 1 ? " · " + card.stats.active + " active" : "") : card.stats.count ? "Last " + card.duration(card.stats.last) : "Not running"
            color: card.stats.active ? theme.accent : theme.muted; font.pixelSize: 12
            Layout.fillWidth: true; wrapMode: Text.WordWrap
        }
        RowLayout {
            id: footer; Layout.fillWidth: true; spacing: 5
            Button { visible: card.configurable; text: card.settingsText; onClicked: card.configure(); font.pixelSize: 12 }
            Button { text: "Timings"; visible: Boolean(card.timingKey); onClicked: card.backend.showTimings(card.timingKey); font.pixelSize: 12 }
        }
    }
}
