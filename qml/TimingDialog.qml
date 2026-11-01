import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "stageTimingDialog"
    required property var backend
    required property var theme
    property string stageKey: ""
    property var rows: backend.timingSteps.filter(step => !stageKey || step.key === stageKey || step.key.indexOf(stageKey + ".") === 0 || (stageKey === "localvision" && (step.key.indexOf("web.") === 0 || step.key === "tagger" || step.key.indexOf("tagger.") === 0 || step.key === "vision" || step.key.indexOf("vision.") === 0)))
    function duration(seconds) {
        if (seconds > 0 && seconds < .001) return "<1 ms"
        if (seconds < 1) return Math.round(seconds * 1000) + " ms"
        if (seconds < 60) return seconds.toFixed(2) + " s"
        return Math.floor(seconds / 60) + "m " + Math.round(seconds % 60) + "s"
    }
    title: stageKey ? "Step timings · " + stageKey : "All evaluation timings"
    modal: true; standardButtons: Dialog.Close
    width: Math.min(800, parent.width - 30); height: Math.min(650, parent.height - 30)
    anchors.centerIn: parent
    contentItem: ColumnLayout {
        spacing: 10
        Label { text: "This app session · averages include completed failed/empty attempts; cancelled calls and cache reuse are separate. Parent/child and parallel timers overlap: do not add them together. Helper-reported substeps arrive when classification finishes."; color: theme.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.maximumWidth: dialog.availableWidth; font.pixelSize: 12 }
        ScrollView {
            id: timingScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            ColumnLayout {
                width: timingScroll.availableWidth - 12; spacing: 7
                Repeater {
                    model: dialog.rows
                    Rectangle {
                        required property var modelData
                        property var stats: (theme.stepTimings || backend.stageTimings)[modelData.key] || ({})
                        Layout.fillWidth: true; implicitHeight: rowBody.implicitHeight + 16
                        radius: 5; color: theme.panel; border.color: stats.active ? theme.accent : theme.line
                        ColumnLayout {
                            id: rowBody; anchors.fill: parent; anchors.margins: 8; spacing: 4
                            Label { text: modelData.label; color: theme.ink; font.bold: Boolean(stats.active); Layout.fillWidth: true; wrapMode: Text.WordWrap }
                            Label { text: stats.count ? "Average " + dialog.duration(stats.average) + " · last " + dialog.duration(stats.last) + " · " + stats.count + " calls" : modelData.label.indexOf("(reported)") >= 0 ? "No helper measurement reported yet" : "No completed calls"; color: theme.muted; font.pixelSize: 12 }
                            Label { visible: Boolean(stats.active); text: "Running " + dialog.duration(stats.elapsed || 0); color: theme.accent; font.pixelSize: 12 }
                            Label { visible: Boolean(stats.cached || stats.cancelled || stats.errors); text: "Cache hits " + (stats.cached || 0) + " · cancelled " + (stats.cancelled || 0) + " · errors " + (stats.errors || 0); color: theme.muted; font.pixelSize: 11 }
                        }
                    }
                }
            }
        }
    }
}
