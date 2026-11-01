import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "previewWindow"
    width: 1280; height: 850
    minimumWidth: 920; minimumHeight: 620
    visible: true
    title: preview.windowTitle
    font.family: "Segoe UI"
    font.pixelSize: 14
    property bool dark: preview.configuration.ui_theme !== "Light"
    readonly property var stepTimings: preview.stageTimings
    property int page: 0
    readonly property color bg: dark ? "#181b20" : "#f6f7f9"
    readonly property color panel: dark ? "#20242b" : "#ffffff"
    readonly property color line: dark ? "#353e49" : "#dce2e9"
    readonly property color ink: dark ? "#e7ebf1" : "#26313f"
    readonly property color muted: dark ? "#acb6c4" : "#626d7c"
    readonly property color accent: dark ? "#8ac9af" : "#256b55"
    color: bg
    palette.window: bg
    palette.windowText: ink
    palette.base: panel
    palette.text: ink
    palette.button: panel
    palette.buttonText: ink
    palette.highlight: accent
    palette.highlightedText: dark ? "#152b22" : "white"

    property real logScrollRatio: 0
    property bool restoringLogScroll: false
    function captureLogScroll() {
        if (!logScroll || !logScroll.contentItem) return
        const view = logScroll.contentItem
        const range = Math.max(0, view.contentHeight - view.height)
        logScrollRatio = range > 0 ? view.contentY / range : 0
        restoringLogScroll = true
    }
    function queueLogScroll() {
        if (logRestoreTimer && followLog && (followLog.checked || restoringLogScroll)) logRestoreTimer.restart()
    }
    function restoreLogScroll() {
        if (!logScroll.contentItem || (!followLog.checked && !restoringLogScroll)) return
        const view = logScroll.contentItem
        const range = Math.max(0, view.contentHeight - view.height)
        view.contentY = followLog.checked ? range : Math.max(0, Math.min(1, logScrollRatio)) * range
        Qt.callLater(function() { restoringLogScroll = false })
    }
    Timer { id: logRestoreTimer; interval: 0; onTriggered: Qt.callLater(window.restoreLogScroll) }

    component Action: Button {
        id: button
        padding: 10
        leftPadding: 13; rightPadding: 13
        background: Rectangle {
            radius: 5
            color: button.checked || button.down ? (window.dark ? "#35453f" : "#dcebe3") : button.hovered ? (window.dark ? "#2b333d" : "#edf1f5") : window.panel
            border.width: 1; border.color: window.line
            opacity: button.enabled ? 1 : .45
        }
        contentItem: Text { text: button.text; font: button.font; color: button.enabled ? window.ink : window.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }

    component Thumb: Rectangle {
        id: thumb
        objectName: external ? "matchedThumb" : "localThumb"
        property string imageUrl: ""
        property string recordId: ""
        property bool external: false
        property bool clickable: true
        readonly property bool ready: picture.status === Image.Ready
        implicitWidth: 132; implicitHeight: 132
        radius: 5; color: window.dark ? "#2a323d" : "#e5eaf0"
        border.color: hover.containsMouse && clickable ? window.accent : window.line
        Image {
            id: picture
            objectName: thumb.external ? "matchedThumbnailImage" : "localThumbnailImage"
            anchors.fill: parent; anchors.margins: 6
            source: thumb.imageUrl
            sourceSize: Qt.size(256, 256)
            fillMode: Image.PreserveAspectFit
            asynchronous: true; cache: true
        }
        Label {
            anchors.centerIn: parent; width: parent.width - 12
            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
            visible: !thumb.imageUrl || picture.status === Image.Error
            text: thumb.external ? "No database preview" : "Preview unavailable"
            color: window.muted; font.pixelSize: 12
        }
        BusyIndicator { anchors.centerIn: parent; width: 32; height: 32; running: picture.status === Image.Loading }
        MouseArea {
            id: hover
            anchors.fill: parent; hoverEnabled: true
            cursorShape: thumb.clickable ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: {
                if (!thumb.clickable) return
                if (thumb.external) preview.openSource(thumb.recordId)
                else preview.openLocal(thumb.recordId)
            }
        }
        ToolTip.visible: hover.containsMouse && thumb.clickable
        ToolTip.text: thumb.external ? "Open matched source page" : "Open local image"
    }

    FileDialog {
        id: historyDialog
        title: "Open saved Tag-AI History"
        fileMode: FileDialog.OpenFile
        nameFilters: ["SQLite history (*.sqlite3 *.db)", "All files (*)"]
        onAccepted: preview.openHistory(selectedFile)
    }

    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 72; color: window.panel
            RowLayout {
                anchors.fill: parent; anchors.margins: 18; spacing: 10
                Label { text: "◇  Tag-AI"; font.pixelSize: 19; font.bold: true; color: window.ink; Layout.rightMargin: 18 }
                Action { text: "Sort"; checked: window.page === 0; onClicked: window.page = 0 }
                Action { text: "History"; checked: window.page === 1; onClicked: window.page = 1 }
                Action { text: "Settings"; checked: window.page === 2; onClicked: window.page = 2 }
                Item { Layout.fillWidth: true }
                Label { text: "QT QUICK"; color: window.accent; font.pixelSize: 11 }
                Action { text: window.dark ? "Light" : "Dark"; onClicked: preview.setSetting("ui_theme", window.dark ? "Light" : "Dark") }
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: window.line }
        RowLayout {
            Layout.fillWidth: true; Layout.margins: 16
            Label { text: preview.status; color: window.muted; elide: Text.ElideRight; Layout.fillWidth: true }
            CheckBox { id: logsToggle; text: "Logs"; objectName: "logsToggle" }
            CheckBox { id: apiToggle; text: "API panel"; objectName: "apiToggle" }
        }
        SplitView {
            id: mainSplit
            objectName: "mainSplitter"
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.leftMargin: 16; Layout.rightMargin: 16
            orientation: Qt.Horizontal
            handle: Rectangle {
                objectName: "mainSplitterHandle"
                implicitWidth: 10; implicitHeight: 10
                color: SplitHandle.pressed ? window.accent : SplitHandle.hovered ? window.line : window.bg
                radius: 3
            }
            StackLayout {
                id: contentPages
                SplitView.fillWidth: true; SplitView.minimumWidth: 460
                SplitView.preferredWidth: 790
                currentIndex: window.page
                SortPane { backend: preview; theme: window }
                Item {
                    ColumnLayout {
                        anchors.fill: parent; spacing: 12
                        Label { text: "History"; color: window.ink; font.pixelSize: 23; font.bold: true }
                        Label { text: "Click your image to open the local file. Click the database match to open its source site."; color: window.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                        Flow {
                            Layout.fillWidth: true; spacing: 8
                            ComboBox { id: scope; model: ["Overall history", "Latest session"]; onActivated: window.applyFilters() }
                            ComboBox { id: category; model: ["All results", "Needs attention", "Moved"]; onActivated: window.applyFilters() }
                            CheckBox { id: attempts; objectName: "repeatAttempts"; text: "Repeated attempts"; onToggled: window.applyFilters() }
                            ComboBox { objectName: "historyOrder"; model: ["Newest first", "Oldest first"]; currentIndex: Math.max(0, model.indexOf(preview.historyOrder)); onActivated: preview.setHistoryOrder(currentText) }
                            Action { text: "Reload saved History"; enabled: !preview.loading; onClicked: preview.loadSaved() }
                        }
                        Flow {
                            Layout.fillWidth: true; spacing: 8
                            Label { text: preview.summary; color: window.accent; topPadding: 10 }
                            Action { objectName: "historyPrevious"; text: "‹ Previous"; enabled: preview.historyPage > 1; onClicked: preview.changeHistoryPage(-1) }
                            Label { text: "Page " + preview.historyPage + " / " + preview.historyPages; color: window.muted; topPadding: 10 }
                            Action { objectName: "historyNext"; text: "Next ›"; enabled: preview.historyPage < preview.historyPages; onClicked: preview.changeHistoryPage(1) }
                        }
                        Label { text: "Review completed images while sorting. The image currently being processed is protected."; color: window.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                        ListView {
                            id: historyList
                            objectName: "historyList"
                            Layout.fillWidth: true; Layout.fillHeight: true
                            model: preview.historyModel
                            property string pageKey: preview.historyPage + ":" + preview.historyOrder + ":" + scope.currentText + ":" + category.currentText + ":" + attempts.checked
                            onPageKeyChanged: Qt.callLater(function() { historyList.positionViewAtBeginning() })
                            clip: true; spacing: 10; cacheBuffer: 350; reuseItems: true
                            boundsBehavior: Flickable.StopAtBounds
                            ScrollBar.vertical: ScrollBar { }
                            delegate: Rectangle {
                                id: card
                                objectName: "historyDelegate"
                                required property string recordId
                                required property string filename
                                required property string folder
                                required property string statusText
                                required property string reasonText
                                required property string dateText
                                required property string localThumbnail
                                required property string matchThumbnail
                                required property bool hasSource
                                required property bool unmatched
                                required property bool canUndo
                                required property bool canUnmatched
                                required property string methodLabel
                                required property string methodColorKey
                                required property bool cachedResult
                                property color methodTint: preview.historyColors[methodColorKey] || "#7d8794"
                                width: historyList.width - 14
                                implicitHeight: rowContent.implicitHeight + 28
                                height: implicitHeight
                                radius: 6
                                color: Qt.tint(window.panel, Qt.rgba(methodTint.r, methodTint.g, methodTint.b, window.dark ? .12 : .09))
                                border.color: Qt.tint(window.line, Qt.rgba(methodTint.r, methodTint.g, methodTint.b, .45))
                                Rectangle { width: 4; radius: 2; anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.margins: 1; color: card.methodTint }
                                ColumnLayout {
                                    id: rowContent
                                    anchors.left: parent.left; anchors.right: parent.right
                                    anchors.top: parent.top; anchors.margins: 14; spacing: 12
                                    RowLayout {
                                        Layout.fillWidth: true; spacing: 16
                                        RowLayout {
                                            spacing: 10; Layout.alignment: Qt.AlignTop
                                            ColumnLayout {
                                                Label { text: "Your image"; color: window.muted; font.pixelSize: 12 }
                                                Thumb { imageUrl: card.localThumbnail; recordId: card.recordId }
                                            }
                                            ColumnLayout {
                                                Label { text: "Database match"; color: window.muted; font.pixelSize: 12 }
                                                Thumb { imageUrl: card.matchThumbnail; recordId: card.recordId; external: true; clickable: card.hasSource }
                                            }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true; Layout.alignment: Qt.AlignTop; spacing: 6
                                            Label { text: card.filename; font.bold: true; color: window.ink; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                                            Flow {
                                                Layout.fillWidth: true; spacing: 6
                                                Rectangle {
                                                    implicitWidth: methodBadge.implicitWidth + 14; implicitHeight: methodBadge.implicitHeight + 8; radius: 4
                                                    color: Qt.rgba(card.methodTint.r, card.methodTint.g, card.methodTint.b, .22)
                                                    Label { id: methodBadge; anchors.centerIn: parent; text: card.methodLabel; color: window.ink; font.pixelSize: 12 }
                                                }
                                                Rectangle {
                                                    visible: card.cachedResult; implicitWidth: cachedBadge.implicitWidth + 14; implicitHeight: cachedBadge.implicitHeight + 8; radius: 4
                                                    color: "transparent"; border.color: window.line
                                                    Label { id: cachedBadge; anchors.centerIn: parent; text: "Cached"; color: window.muted; font.pixelSize: 12 }
                                                }
                                            }
                                            Label { text: card.dateText; color: window.muted; font.pixelSize: 12 }
                                            Label { text: card.statusText; color: card.unmatched ? (window.dark ? "#e8bd7d" : "#906223") : window.accent; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                            Label { text: card.folder; color: window.muted; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideMiddle; Layout.fillWidth: true }
                                            Label { text: card.reasonText; color: window.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                        }
                                    }
                                    Flow {
                                        Layout.fillWidth: true; spacing: 8
                                        Action { text: "Show in folder"; onClicked: preview.showFolder(card.recordId) }
                                        Action { text: "Open source page"; enabled: card.hasSource; onClicked: preview.openSource(card.recordId) }
                                        Action { text: "To Unmatched"; visible: card.canUnmatched; enabled: !preview.editing; onClicked: preview.historyAction(card.recordId, "unmatched") }
                                        Action { text: "Undo move"; visible: card.canUndo; enabled: !preview.editing; onClicked: preview.historyAction(card.recordId, "undo") }
                                        Action { text: "Clear cached searches"; enabled: !preview.editing; onClicked: preview.historyAction(card.recordId, "cache") }
                                        Action { text: "Remove tags…"; enabled: !preview.editing; onClicked: { removeTagsDialog.recordId = card.recordId; removeTagsDialog.open() } }
                                    }
                                }
                            }
                            Label { anchors.centerIn: parent; text: preview.loading ? "Loading…" : "No results in this view."; visible: historyList.count === 0; color: window.muted }
                        }
                    }
                }
                SettingsPane { backend: preview; theme: window }
            }
            SplitView {
                id: diagnostics
                objectName: "diagnosticsSplitter"
                visible: logsToggle.checked || apiToggle.checked
                orientation: Qt.Vertical
                SplitView.preferredWidth: 430; SplitView.minimumWidth: 280
                handle: Rectangle { objectName: "diagnosticsSplitterHandle"; implicitWidth: 10; implicitHeight: 10; radius: 3; color: SplitHandle.hovered ? window.line : window.bg }
                Rectangle {
                    visible: apiToggle.checked
                    SplitView.minimumHeight: 110; SplitView.preferredHeight: 210
                    SplitView.fillHeight: !logsToggle.checked
                    color: window.panel; radius: 6
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 14
                        Label { text: "API usage"; color: window.ink; font.bold: true; font.pixelSize: 17 }
                        ScrollView {
                            id: usageScroll
                            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                            Label { width: usageScroll.availableWidth; text: preview.actualUsage; color: window.muted; wrapMode: Text.WordWrap }
                        }
                    }
                }
                Rectangle {
                    visible: logsToggle.checked
                    SplitView.minimumHeight: 150; SplitView.fillHeight: true
                    color: window.panel; radius: 6
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 12
                        RowLayout {
                            Label { text: "Activity log"; color: window.ink; font.pixelSize: 17; font.bold: true; Layout.fillWidth: true }
                            CheckBox { id: detailedLog; objectName: "detailedLog"; text: "Detailed"; onPressedChanged: if (pressed) window.captureLogScroll(); onToggled: window.queueLogScroll() }
                            CheckBox { id: followLog; objectName: "followLog"; text: "Follow"; checked: true; onToggled: if (checked) window.queueLogScroll() }
                        }
                        ScrollView {
                            id: logScroll
                            objectName: "logScroll"
                            onAvailableHeightChanged: window.queueLogScroll()
                            onVisibleChanged: if (visible) window.queueLogScroll()
                            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                            TextArea { id: logArea; objectName: "logArea"; onTextChanged: window.queueLogScroll(); onContentHeightChanged: window.queueLogScroll(); width: logScroll.availableWidth; text: detailedLog.checked ? preview.logText : preview.summaryLog; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: window.ink; font.family: "Consolas"; font.pixelSize: 12; background: Rectangle { color: window.panel } }
                        }
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.margins: 12
            Label { text: preview.details; color: window.muted; font.pixelSize: 12; Layout.fillWidth: true }
            Label { text: "QT QUICK"; color: window.accent; font.pixelSize: 11 }
        }
    }
    function applyFilters() {
        if (category && scope && attempts) preview.setFilters(category.currentText, scope.currentText, attempts.checked)
    }
    Connections {
        target: preview
        function onLogsChanged() {
            window.queueLogScroll()
        }
    }
    Dialog {
        id: removeTagsDialog
        property string recordId: ""
        title: "Remove embedded app tags?"
        modal: true; standardButtons: Dialog.Ok | Dialog.Cancel
        Label { width: 350; wrapMode: Text.WordWrap; text: "Remove the application's embedded tags from this image? Unrelated metadata and image pixels are preserved." }
        onAccepted: preview.historyAction(recordId, "tags")
    }
    onClosing: close => {
        close.accepted = false
        preview.requestClose()
    }
}
